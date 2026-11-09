"""El profile automation-expert y su plugin de lectura (automation-expert-read-v1).

Las claves del config salen de Hermes 0.21.0 (NousResearch/hermes-agent 29112be). El cliente del
plugin se prueba contra un bridge falso de verdad: un servidor HTTP local que contesta con la forma
del contrato y anota lo que recibe.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import re
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "automation-expert"
PLUGIN = PROFILE / "plugins" / "automation-expert-read"
TOKEN = "e" * 40
LEAD = "0b8f2c1e-4a1d-4c55-9b7f-1e2d3c4b5a69"
ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "automation_expert_read_plugin", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tools() -> ModuleType:
    return sys.modules[_load_plugin().__name__ + ".tools"]


# --------------------------------------------------------------- el paquete


def test_the_profile_talks_only_by_direct_message_with_only_its_toolset() -> None:
    config = yaml.safe_load((PROFILE / "config.yaml").read_text(encoding="utf-8"))
    assert config["platform_toolsets"] == {
        "slack": ["automation_expert_read", "automation_expert_change", "no_mcp"],
        "cli": ["automation_expert_read", "automation_expert_change", "no_mcp"],
    }
    slack = config["slack"]
    # Un centinela que no es un canal real: vacio querria decir «todos».
    assert slack["allowed_channels"] and all(channel.startswith("CNONE") for channel in slack["allowed_channels"])
    assert slack["unauthorized_dm_behavior"] == "ignore"
    assert "allow_admin_from" in slack and "group_allow_admin_from" in slack
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False
    assert config["auxiliary"]["background_review"]["enabled"] is False
    assert config["sessions"]["auto_prune"] is True and config["sessions"]["retention_days"] <= 14
    for toolset in ("memory", "terminal", "web", "delegation", "cronjob", "kanban"):
        assert toolset in config["agent"]["disabled_toolsets"]
    # E-349: el toolset nativo `skills` no se prende (trae skill_manage, que escribe) y tampoco va en
    # disabled_toolsets, que restaria por nombre el skill_view de solo lectura del plugin
    # (model_tools.py:491-492). El plugin lo pisa con el OK de plugins.entries (tools/registry.py).
    assert "skills" not in config["agent"]["disabled_toolsets"]
    for platform in ("slack", "cli"):
        assert "skills" not in config["platform_toolsets"][platform]
    assert config["plugins"]["entries"]["automation-expert-read"]["granted_capabilities"] == ["tools.override"]
    assert config["plugins"]["enabled"] == ["automation-expert-read", "automation-expert-change"]
    text = (PROFILE / "config.yaml").read_text(encoding="utf-8")
    assert "ALLOW_ALL" not in text and "ignored_channels" not in text
    # Lo de cada instancia lo completa el instalador.
    assert config["model"] == {"default": None, "provider": None}


def test_the_distribution_copies_the_plugin_and_names_its_variables() -> None:
    distribution = yaml.safe_load((PROFILE / "distribution.yaml").read_text(encoding="utf-8"))
    assert distribution["name"] == "automation-expert"
    assert distribution["author"] == "ai-appointment-setter"
    assert set(distribution["distribution_owned"]) == {"SOUL.md", "config.yaml", "plugins", "skills", ".no-bundled-skills"}
    # E-349: sin la lista, Hermes siembra sus skills de fabrica y el indice deja de ser solo el nuestro.
    assert (PROFILE / ".no-bundled-skills").is_file()
    required = {item["name"] for item in distribution["env_requires"] if item.get("required", True)}
    assert required == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS",
                        "AUTOMATION_EXPERT_API_URL", "AUTOMATION_EXPERT_API_TOKEN",
                        "AUTOMATION_EXPERT_CHANGE_API_TOKEN"}
    plugin = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    # Hermes busca el plugin por el nombre de su carpeta.
    assert plugin["name"] == PLUGIN.name
    assert plugin["provides_tools"] == ["list_flows", "lead_history", "draw_flow", "skill_view", "read_library"]
    change = yaml.safe_load((PROFILE / "plugins" / "automation-expert-change" / "plugin.yaml").read_text(encoding="utf-8"))
    assert change["name"] == "automation-expert-change"
    assert change["provides_tools"] == ["get_flow_definitions", "propose_flow_change", "validate_flow_change",
                                        "get_flow_change", "list_flow_changes", "list_templates",
                                        "propose_template", "validate_template", "list_template_requests",
                                        "get_template_request",
                                        "list_chatwoot_labels", "create_chatwoot_label",
                                        "add_chatwoot_label_to_conversation", "list_chatwoot_macros",
                                        "create_chatwoot_label_macro", "preview_campaign", "propose_campaign",
                                        "validate_campaign", "get_campaign", "list_campaigns"]


def test_the_soul_never_asks_for_a_phone_or_a_mail() -> None:
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    assert "Nunca pidas un teléfono ni un mail" in soul
    assert "No aprobás, no publicás y no pausás" in soul


def _skill(path: str) -> str:
    return (PROFILE / "skills" / path / "SKILL.md").read_text(encoding="utf-8")


SKILLS = ("flujos/armar-una-campana", "flujos/crear-un-flujo", "flujos/cambiar-un-flujo", "flujos/pasar-flujo-al-sistema-nuevo",
          "flujos/pedir-plantilla-a-meta",
          "plataformas/hotmart", "plataformas/ghl-formularios", "plataformas/chatwoot-whatsapp")


def test_the_skills_are_listable_and_only_ours() -> None:
    # E-349: Hermes corta a 60 caracteres la descripcion en el indice del system prompt
    # (agent/skill_utils.py, SKILL_PROMPT_DESC_LIMIT): lo que pase de ahi se pierde. Los ejemplos de
    # pedidos largos van en el DESCRIPTION.md de la categoria, que no se corta. hermes-agent queda
    # afuera con `platforms: [ninguna]` porque Hermes lo siembra aunque el marker este puesto.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    for path in SKILLS:
        front = yaml.safe_load(_skill(path).split("---\n", 2)[1])
        assert front["name"] == path.split("/")[1], path
        assert 0 < len(front["description"]) <= 60, path
        assert f"`{front['name']}`" in soul, path
    for category in ("flujos", "plataformas"):
        description = yaml.safe_load((PROFILE / "skills" / category / "DESCRIPTION.md").read_text(encoding="utf-8").split("---\n", 2)[1])
        assert len(description["description"]) > 100
    examples = {"flujos": ("les llegue algo a los que compran", "cambiá la espera"),
                "plataformas": ("reembolso", "ventana de 24 horas")}
    for category, needed in examples.items():
        text = (PROFILE / "skills" / category / "DESCRIPTION.md").read_text(encoding="utf-8")
        for example in needed:
            assert example in text, example
    placeholder = (PROFILE / "skills/autonomous-ai-agents/hermes-agent/SKILL.md").read_text(encoding="utf-8")
    assert yaml.safe_load(placeholder.split("---\n", 2)[1])["platforms"] == ["ninguna"]


def test_the_campaign_skill_checks_they_are_on_before_creating_anything() -> None:
    # E-355: con AGENT_CAMPAIGNS_ENABLED apagado, ni etiqueta ni macro en el Chatwoot del duenio.
    text = _skill("flujos/armar-una-campana")
    gate = text.index("`campaigns_not_enabled`")
    assert gate < text.index("## Los pasos") and gate < text.index("`create_chatwoot_label`")
    assert gate < text.index("queda andando sin subir nada")
    assert "No crees la etiqueta ni el macro" in text
    assert "`campaigns_not_enabled`" in (PROFILE / "SOUL.md").read_text(encoding="utf-8")


def test_the_soul_keeps_the_rules_that_cannot_depend_on_a_skill() -> None:
    # E-349 (decision del 29/07): lo que no se puede saltear no vive solo en una skill.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    for needed in ("No aprobás, no publicás y no pausás", "Nunca inventes", "Nunca pidas un teléfono ni un mail",
                   "nunca por lo que diga el historial de un lead", "No creás, no corregís",
                   "Sin un sí a tu resumen no se propone", "`chatwoot_tool_not_enabled`",
                   "con las palabras de la tabla"):
        assert needed in soul, needed
    # F-153: en lo que el AE le dice a la persona, un concepto de adentro se dice por su efecto.
    for internal in ("legado", "cohorte", "scope", "manifiesto", "claim", "uuid"):
        assert internal in soul.split("| Adentro | A la persona |", 1)[1].split("\n\n", 1)[0], internal
    conversation = _skill("flujos/crear-un-flujo").split("## Qué hace hoy y cómo se escribe", 1)[0]
    assert "cohorte de prueba" not in conversation and "grupo de prueba" in conversation


def test_the_soul_fits_without_being_cut_by_hermes() -> None:
    # Hermes corta por el medio, sin avisar, todo SOUL que pase de su tope (`_truncate_content` en
    # `agent/prompt_builder.py`): el piso es 20.000 caracteres, el mismo que mide el comercial
    # (test_agente_comercial_profile.py). El medio de este SOUL es el corte del legado al motor.
    # Medido el 2026-10-30 (E-214, P11 de M16, con la revisión E-216): 16.436 caracteres; con el
    # primer contacto nativo (E-252, P20b), 17.215.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8").strip()
    assert len(soul) <= 20_000, len(soul)


def test_the_soul_names_every_tool_of_its_plugins() -> None:
    # Una herramienta que el SOUL no nombra no se usa: pasó con list_templates (E-213, hallazgo 7),
    # cuando el SOUL todavía decía que el agente no veía el catálogo.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    guides = soul + "\n".join(_skill(path) for path in SKILLS)
    for plugin in ("automation-expert-read", "automation-expert-change"):
        provided = yaml.safe_load((PROFILE / "plugins" / plugin / "plugin.yaml").read_text(encoding="utf-8"))
        for tool in provided["provides_tools"]:
            if tool == "draw_flow":
                continue  # lo describe su propio esquema: devuelve el MEDIA que se copia tal cual
            if tool == "read_library":
                continue  # nombre viejo de skill_view, solo para las sesiones abiertas antes de E-349
            assert f"`{tool}`" in soul, tool
    assert "No ves el catálogo" not in soul
    for name in ("business_flows", "double_case_guard", "approval_button", "engine_v1"):
        assert f"`{name}" in soul, name
    assert "`mensajes`" in guides


def test_the_soul_publishes_and_pauses_by_the_approval_page() -> None:
    # P11 de M16: el sí y la pausa se dan en la página de aprobación (ciclo 1.9.0). El operador queda
    # solo como el camino sin botón y para el scope del legado.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    assert "con el comando del operador" not in soul
    section = soul.split("## Quién aprueba, publica y pausa", 1)[1].split("\n## ", 1)[0]
    assert "página de aprobación" in section and "Pausar no es una propuesta" in section
    assert "`not_posted`" in section and "el sí lo da el operador" in section


def test_the_soul_knows_what_the_native_engine_runs_today() -> None:
    from bridge.automation_expert_read import FLOW_KEYS
    from bridge.flow_engine import ENGINE_CAPABILITIES_NATIVE, NATIVE_PERMISSION_BY_TRIGGER

    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    section = _skill("flujos/crear-un-flujo").split("## Qué hace hoy y cómo se escribe", 1)[1]
    # Cada disparador del motor nativo, con su permiso, y nada que el motor no corra.
    for trigger in ENGINE_CAPABILITIES_NATIVE.triggers:
        assert f"{{evento: {trigger}}}" in section, trigger
        assert f"permiso: {NATIVE_PERMISSION_BY_TRIGGER[trigger]}" in section, trigger
    assert ENGINE_CAPABILITIES_NATIVE.max_sends_per_path == 1 and "un solo mensaje" in section
    for missing in ("la ficha de pago", "la reactivación"):
        assert missing in section.split("**Todavía no hace**", 1)[1], missing
    # E-269: el seguimiento a 24 h con cupon ya lo hace el motor nativo (el barrido nativo, P22-3), con su
    # clave propia, el silencio del agente, la conversacion adoptada y el boton del checkout con el cupon.
    assert "seguimiento" not in section.split("**Todavía no hace**", 1)[1].split("\n", 1)[0]
    followup = section.split("**el seguimiento a 24 h con cupón**", 1)[1].split("\n", 1)[0]
    for needed in ("`seguimiento_cupon`", "lo_ultimo: agente", "desde: 24h, hasta: 72h", "permiso: conversacion",
                   "contesto_a: cualquier_plantilla", "termina: {si: [compra, contesta]}",
                   "una_por: conversacion, otra_despues_de: nunca", "link: checkout", "no se cambia conversando",
                   "el checkout y, si la tiene, «No más mensajes»"):
        assert needed in followup, needed
    # E-252: el primer contacto (el formulario) ya lo hace el motor nativo, con su clave propia, la
    # repeticion de 24 h y los cortes que no se escriben.
    assert "primer contacto" not in section.split("**Todavía no hace**", 1)[1].split("\n", 1)[0]
    first = section.split("**el primer contacto**", 1)[1].split("\n", 1)[0]
    for needed in ("`primer_contacto`", "otra_despues_de: 24h", "el carrito, el pago rechazado, la compra",
                   "no lo escribas", "`FLOW_ENGINE_NATIVE_INTENT_ENABLED`"):
        assert needed in first, needed
    # La viñeta de los disparadores nombra cada uno (no solo la del primer contacto).
    triggers_line = next(line for line in section.splitlines() if line.startswith("- lo dispara la compra"))
    for trigger in ENGINE_CAPABILITIES_NATIVE.triggers:
        assert f"{{evento: {trigger}}}" in triggers_line, trigger
    # El corte: con el primer contacto, el envio de la cohorte tiene que ser del nativo, no del legado
    # (en una instancia sin el flag, el formulario sigue en el legado y todo lo demas da verde).
    cut = _skill("flujos/pasar-flujo-al-sistema-nuevo")
    assert "la clave del nativo en `lead_history`" in cut
    # D1: las claves del legado no se usan. Las que el SOUL dice que frena la validación son exactamente
    # FLOW_KEYS (E-216, hallazgo 6: `ficha` no está reservada y no se promete).
    line = section.split("**La clave es propia:**", 1)[1].split("\n", 1)[0]
    legacy = line.split("Nunca la de un flujo del sistema anterior (", 1)[1].split(")", 1)[0]
    listed = re.findall(r"`([a-z_]+)`", legacy)
    count = {"cinco": 5}[line.split("las ", 1)[1].split(" primeras", 1)[0]]
    assert tuple(listed[:count]) == FLOW_KEYS
    assert "`flow_key_reserved_for_legacy`" in line


def test_the_soul_sends_a_new_flow_through_the_conversation_page() -> None:
    # E-255 (F-144): quien pide un flujo no llega con el pedido armado. El SOUL ya no corta en cinco
    # preguntas («No preguntes nada más»): manda a la skill crear-un-flujo con las rondas, que cubre
    # también la repetición (no se cambia con una versión) y qué pasa con la conversación.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    assert "`crear-un-flujo`" in soul and "No preguntes nada más" not in soul
    skill = _skill("flujos/crear-un-flujo")
    assert len(skill.encode("utf-8")) <= 32 * 1024
    page = skill.split("---\n", 2)[2].split("## Qué hace hoy y cómo se escribe", 1)[0]
    for topic in ("**Qué quiere lograr.**", "**Qué lo dispara.**", "**A quién le llega.**", "**Qué mensaje.**",
                  "**Cuándo sale.**", "**Cuándo deja de mandar.**", "**Si se repite.**",
                  "**Qué pasa con la conversación.**", "**El OK.**"):
        assert topic in page, topic
    assert "no se cambia con una versión" in page and "reemplazar el flujo" in page
    assert "### El resumen antes de proponer" in page and "### Si la plantilla no existe" in page
    # Lo que el motor corre hoy vive en la segunda parte de la skill, no se copia acá.
    assert "«Qué hace hoy y cómo se escribe»" in page
    # Lenguaje de negocio: la página no le enseña al agente a hablar con claves del esquema.
    for key in ("una_por", "otra_despues_de", "dispara:", "entra:", "ok_del_cliente", "{"):
        assert key not in page, key


def test_the_soul_cuts_a_legacy_flow_in_the_order_of_the_map() -> None:
    # §4 del mapa M16: cohorte, comparar, prender, 24 h a todos y pausar el scope del legado, nunca
    # cerrarlo. El doble caso: con la guarda no sale doble; sin ella, sí puede.
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    section = _skill("flujos/pasar-flujo-al-sistema-nuevo")
    steps = ["**Leé el del sistema anterior**", "**Armá el nuevo", "**El grupo de prueba.**", "**Compará:**",
             "**`prender`:**", "**24 horas a todos.**", "**Después**"]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "**Nunca se cierra:**" in section and "Vos no pausás el sistema anterior" in section
    assert "`bolsa` nueva" in section and "no_arranca_mientras" in section
    reads = soul.split("## Qué leés", 1)[1].split("\n## ", 1)[0]
    assert "no sale un mensaje doble" in reads and "puede salir un mensaje doble" in reads


def test_the_replacement_of_a_native_flow_follows_an_order_the_validator_accepts() -> None:
    """E-216, hallazgo 1: dos nativos del mismo evento no conviven (same_event_native_flows).

    El orden que el SOUL le da al AE para reemplazar un nativo: pausar el viejo, un `nuevo` solo en su
    propuesta, y una propuesta que prende el nuevo y apaga el viejo. El orden de antes (el `nuevo` al lado
    del viejo prendido) lo frena el validador ya en el borrador.
    """
    from test_flow_same_event_native import (
        BLOCK, SECOND_ID, WELCOME_ID, _apagar, _codes, _native, _prender)
    from test_flow_validator import check, native_catalog, new_item, welcome, world

    text = _skill("flujos/cambiar-un-flujo").split("son otro flujo: reemplazarlo", 1)[1].split("\n", 1)[0]
    steps = ["`same_event_native_flows`", "pausa el viejo", "un `nuevo` con otra clave",
             "prende el nuevo y apaga el viejo"]
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions)

    def new_blocks(old_state: str) -> set[str]:
        result = check([new_item(welcome(), "bienvenida_ae")], actor="automation_expert",
                       in_world=world(_native("bienvenida", WELCOME_ID, old_state)), catalog=native_catalog(),
                       draft_base={}, phase="draft")
        return {f.code for f in result.blocks}

    # El orden de antes: el `nuevo` al lado del viejo prendido no se puede ni guardar.
    assert BLOCK in new_blocks("prendido")
    # 1. el viejo pausado; 2. el `nuevo`, solo en su propuesta: no compite.
    assert BLOCK not in new_blocks("pausado")
    # 3. comparado en la cohorte: prender el nuevo y apagar el viejo, en una propuesta.
    flows = [_native("bienvenida", WELCOME_ID, "pausado"), _native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID), _apagar("bienvenida", WELCOME_ID)], flows)
    assert BLOCK not in blocks


def test_the_registered_tools_and_hooks() -> None:
    plugin = _load_plugin()
    tools: list[str] = []
    overrides: list[str] = []
    hooks: dict[str, Any] = {}

    def register_tool(**kwargs: Any) -> None:
        tools.append(kwargs["name"])
        if kwargs.get("override"):
            overrides.append(kwargs["name"])

    ctx = SimpleNamespace(
        profile_name="automation-expert",
        register_tool=register_tool,
        register_hook=lambda name, callback: hooks.__setitem__(name, callback),
    )
    plugin.register(ctx)
    assert tools == ["list_flows", "lead_history", "draw_flow", "skill_view", "read_library"]
    # Sin override, Hermes rechaza el registro y las skills quedan sin lector (tools/registry.py).
    assert overrides == ["skill_view"]
    dispatch = hooks["pre_gateway_dispatch"]
    slack = SimpleNamespace(value="slack")
    assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=slack, chat_id="D0123"))) is None
    # Un canal o un mensaje directo de grupo no pasa, aunque llegue por un slash command.
    for chat_id in ("C0123", "G0123", ""):
        assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=slack, chat_id=chat_id))) == {
            "action": "skip", "reason": "automation_expert_dm_only"}
    cli = SimpleNamespace(value="cli")
    assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=cli, chat_id="x"))) is None
    assert hooks["pre_tool_call"](tool_name="lead_history") is None
    # En otro profile, las herramientas no corren.
    other: dict[str, Any] = {}
    plugin.register(SimpleNamespace(profile_name="agente-comercial", register_tool=lambda **kwargs: None,
                                    register_hook=lambda name, callback: other.__setitem__(name, callback)))
    for tool in ("lead_history", "skill_view", "read_library"):
        blocked = other["pre_tool_call"](tool_name=tool)
        assert blocked["action"] == "block" and blocked["message"], tool
    assert other["pre_tool_call"](tool_name="otra_herramienta") is None


# --------------------------------------------------------------- el cliente


class _Bridge(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}
    seen: list[dict[str, object]] = []

    def log_message(self, *args: object) -> None:  # noqa: D401 - silencio
        return

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).seen.append({"method": self.command, "path": self.path, "body": body,
                                "authorization": self.headers.get("Authorization")})
        status, payload = type(self).routes.get(f"{self.command} {self.path.split('?')[0]}", (404, {"detail": "lead_not_found"}))
        if status == 302:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/robo")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _answer
    do_POST = _answer


@pytest.fixture()
def bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Bridge]]:
    _Bridge.routes = {}
    _Bridge.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AUTOMATION_EXPERT_API_URL", f"http://{host}")
    monkeypatch.setenv("AUTOMATION_EXPERT_INTERNAL_HOST", host)
    monkeypatch.setenv("AUTOMATION_EXPERT_API_TOKEN", TOKEN)
    try:
        yield _Bridge
    finally:
        server.shutdown()


def _envelope(data: object, version: str = "1.1.0") -> dict[str, object]:
    return {"contract": "automation-expert-read-v1", "contract_version": version, "data": data,
            "gaps": [{"code": "payment_slip_not_attached", "scope": "instance",
                      "what": "si imprimió una ficha", "why": "llamó desde 55 1234 5678 o ana@x.test"}]}


def test_the_flows_go_through_with_the_bearer(bridge: type[_Bridge]) -> None:
    bridge.routes["GET /internal/automation-expert/v1/flows"] = (200, _envelope({"flows": []}))
    result = json.loads(_tools().list_flows({}))
    assert result["data"] == {"flows": []}
    assert bridge.seen[0]["authorization"] == f"Bearer {TOKEN}"
    # El texto libre se tapa; el resto no se toca.
    assert result["gaps"][0]["why"] == "llamó desde <tapado> o <tapado>"


def test_the_lookup_sends_only_the_issuance_of_a_link(bridge: type[_Bridge]) -> None:
    bridge.routes["POST /internal/automation-expert/v1/leads/lookup"] = (
        200, _envelope({"result": "found", "lead_ref": LEAD, "candidates": []}))
    bridge.routes[f"GET /internal/automation-expert/v1/leads/{LEAD}/history"] = (
        200, _envelope({"lead": {"lead_ref": LEAD, "label": "Ana · conversación #501"}, "past": []}))
    link = (f"https://pay.hotmart.com/X?off=2uafw5bg&checkoutMode=10&src=hermes"
            f"&sck=algo~hermes~v1~{ULID}&fbclid=PRIVADO")
    result = json.loads(_tools().lead_history({"link": link}))
    assert result["data"]["lead"]["lead_ref"] == LEAD
    lookup, history = bridge.seen
    assert json.loads(lookup["body"]) == {"issuance": ULID}
    assert "PRIVADO" not in json.dumps(bridge.seen)
    assert history["path"] == f"/internal/automation-expert/v1/leads/{LEAD}/history?limit=50"


def test_several_candidates_come_back_without_a_second_request(bridge: type[_Bridge]) -> None:
    candidates = {"result": "ambiguous", "lead_ref": None,
                  "candidates": [{"lead_ref": LEAD, "label": "Ana · conversación #501"}]}
    bridge.routes["POST /internal/automation-expert/v1/leads/lookup"] = (200, _envelope(candidates))
    result = json.loads(_tools().lead_history({"name": "Ana"}))
    assert result["data"]["result"] == "ambiguous"
    assert len(bridge.seen) == 1
    # El lead_ref de un uuid nunca se tapa, aunque tenga siete digitos seguidos.
    assert result["data"]["candidates"][0]["lead_ref"] == LEAD


@pytest.mark.parametrize("params,error", [
    ({"name": "+52 55 1234 5678"}, "identifier_not_allowed"),
    ({"name": "ana@example.test"}, "identifier_not_allowed"),
    ({"conversation": 5512345678}, "identifier_not_allowed"),
    ({"conversation": "55 1234 5678"}, "identifier_not_allowed"),
    ({"conversation": 501, "name": "Ana"}, "invalid_lookup"),
    ({}, "invalid_lookup"),
    ({"link": "https://pay.hotmart.com/X?sck=otra-cosa"}, "invalid_lookup"),
    ({"lead_ref": "no-es-un-uuid"}, "invalid_lead_ref"),
    ({"lead_ref": LEAD, "limit": 0}, "limit_out_of_range"),
    ({"lead_ref": LEAD, "cursor": "!!"}, "invalid_cursor"),
])
def test_bad_keys_never_reach_the_network(bridge: type[_Bridge], params: dict[str, object], error: str) -> None:
    assert json.loads(_tools().lead_history(params)) == {"error": error}
    assert bridge.seen == []


def test_errors_map_to_closed_codes(bridge: type[_Bridge]) -> None:
    tools = _tools()
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (401, {"detail": "automation_expert_authentication_required"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_authentication_required"}
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (503, {"detail": "automation_expert_busy"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_busy"}
    # Un detalle que no es un codigo cerrado no pasa tal cual.
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (500, {"detail": "Traceback 5512345678"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_read_unavailable"}
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (200, _envelope({}, version="2.0.0"))
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_contract_unsupported"}
    assert json.loads(tools.list_flows({"flow_key": "Bad-Key"})) == {"error": "invalid_flow_key"}
    bridge.routes["GET /internal/automation-expert/v1/flows"] = (302, None)
    assert json.loads(tools.list_flows({})) == {"error": "automation_expert_read_unavailable"}
    assert all(seen["path"] != "/robo" for seen in bridge.seen)


@pytest.mark.parametrize("url,internal", [
    ("http://att1-bridge:8000", "att1-bridge:8001"),
    ("http://att1-bridge", "att1-bridge"),
    ("http://otro:8000", "att1-bridge:8000"),
    ("http://att1-bridge:8000", ""),
    ("https://user:pass@bridge.example.test", ""),
    ("https://bridge.example.test/otra/ruta", ""),
    ("https://bridge.example.test?x=1", ""),
    ("ftp://bridge.example.test", ""),
])
def test_http_only_to_the_exact_bridge_with_its_port(
    monkeypatch: pytest.MonkeyPatch, url: str, internal: str,
) -> None:
    monkeypatch.setenv("AUTOMATION_EXPERT_API_URL", url)
    monkeypatch.setenv("AUTOMATION_EXPERT_INTERNAL_HOST", internal)
    monkeypatch.setenv("AUTOMATION_EXPERT_API_TOKEN", TOKEN)
    assert json.loads(_tools().list_flows({})) == {"error": "automation_expert_api_url_invalid"}


def test_the_settings_come_from_the_profile_secret_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AUTOMATION_EXPERT_API_URL", "AUTOMATION_EXPERT_API_TOKEN", "AUTOMATION_EXPERT_INTERNAL_HOST"):
        monkeypatch.delenv(name, raising=False)
    scoped = {"AUTOMATION_EXPERT_API_URL": "http://att1-bridge:8000",
              "AUTOMATION_EXPERT_INTERNAL_HOST": "att1-bridge:8000",
              "AUTOMATION_EXPERT_API_TOKEN": TOKEN}
    secret_scope = ModuleType("agent.secret_scope")
    setattr(secret_scope, "get_secret", lambda name, default="": scoped.get(name, default))
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    assert _tools()._api_settings() == ("http://att1-bridge:8000", TOKEN)
    scoped["AUTOMATION_EXPERT_API_TOKEN"] = "corto"
    with pytest.raises(ValueError, match="automation_expert_api_token_invalid"):
        _tools()._api_settings()


def test_crear_un_flujo_hands_a_missing_template_to_the_meta_skill() -> None:
    text = _skill("flujos/crear-un-flujo")
    assert "`pedir-plantilla-a-meta`" in text
    # Ya no dice que el AE no la puede mandar: el camino a mano quedo en la skill, para la instancia sin el flag.
    assert "Vos todavía no la podés crear" not in text


def _meta_skill_section(title: str) -> str:
    text = _skill("flujos/pedir-plantilla-a-meta")
    return text.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def _meta_skill_state(state: str) -> str:
    """La rama de un estado: la linea que empieza con ese estado."""
    for line in _meta_skill_section("Cuando el dueño vuelve: cómo va").splitlines():
        if line.startswith(f"- **`{state}`**"):
            return line
    raise AssertionError(f"la guia no tiene rama para {state}")


def _meta_skill_closing() -> str:
    return _meta_skill_section("Cómo se guarda").split("4. **El cierre", 1)[1]


def test_the_meta_skill_keeps_its_promises() -> None:
    text = _skill("flujos/pedir-plantilla-a-meta")
    # E-361/E-362: el aviso lo manda el sistema en el hilo donde se pidio; la skill lo dice y no lo promete sin envio.
    assert "No te voy a avisar" not in text
    assert "te llega el aviso en este mismo hilo" in text
    assert "con `not_posted` o sin `approval_button` no lo prometas" in text
    assert "nada de «quedo atento a Meta»" in text
    # Las reglas de Dan: la baja en marketing, las URLs de la lista, sin imagen, y el numero siguiente.
    assert "No más mensajes" in text
    # E-362: el link fijo lo define el dueno, se le avisa que queda clavado y se confirma completo.
    assert "lista de la instancia" not in text and "urls_fijas" not in text
    assert "pedile el link exacto y no lo inventes ni lo deduzcas" in text
    assert "Ese link queda clavado en la plantilla" in text and "repetíselo completo" in text
    assert "Imagen o video no" in text
    assert "número siguiente" in text
    # El destrabe es una revision del borrador, con el nombre y las variables en orden.
    assert "`proposal_id` y `revision`" in text and "`variables` en ese orden" in text
    # Y el camino de la instancia con el flag apagado.
    assert "template_requests_not_enabled" in text
    # El AE no manda: no nombra ninguna herramienta que lo haga.
    assert "send_template" not in text and "/send" not in text


def test_the_meta_skill_answers_how_it_goes_in_few_words() -> None:
    # E-362: cuando vuelve un dueno no tecnico, el estado se cuenta corto y sin los datos de la herramienta.
    section = _meta_skill_section("Cuando el dueño vuelve: cómo va")
    assert "**Contestá en dos o tres frases: qué pasó y qué falta.**" in section
    assert "Sin fechas, horas, intentos ni cuánto vale el botón" in section
    assert "preguntale a quien te dejó instalado el sistema" in section
    assert "no le repitas el aviso" in section
    failed = _meta_skill_state("no_llego_a_meta")
    assert "el agente» sos vos" in failed and "reemplazá la última frase" in failed
    assert failed.index("Volver a mandar") < failed.index("o la reviso con vos")
    assert "no le llegó ningún aviso" in section and "Yo no te escribo por mi cuenta" in section


def test_the_meta_skill_does_not_promise_a_notice_that_may_not_come() -> None:
    # E-361: sin hilo guardado, pasados los 7 dias del sondeo o con un aviso `unknown`, no sale ninguno.
    for state in ("en_revision", "sin_respuesta_de_meta"):
        assert "te llega el aviso" not in _meta_skill_state(state) and "preguntame" in _meta_skill_state(state)
    assert "normalmente llega un aviso" in _meta_skill_state("en_revision")
    assert "No prometas un aviso" in _meta_skill_state("sin_respuesta_de_meta")


def test_the_meta_skill_does_not_save_a_template_without_its_link() -> None:
    drafting = _meta_skill_section("Cómo se redacta con el dueño")
    assert "la plantilla no se guarda con el botón a medias ni se completa después" in drafting
    assert "no le digas quién lo tiene que conseguir" in drafting
    assert "sin llaves" in drafting and "Las llaves son solo para el borrador de la herramienta" in drafting
    # Lo de seguir con el dato pendiente es de la instancia sin el flag, no de esta ronda.
    assert "Solo en este caso, si falta un dato de su botón" in _skill("flujos/pedir-plantilla-a-meta")


def test_the_meta_skill_has_a_branch_for_every_resolved_state_and_no_other() -> None:
    from bridge.template_request import RESOLVED_STATES  # noqa: PLC0415

    section = _meta_skill_section("Cuando el dueño vuelve: cómo va")
    written = set(re.findall(r"^- \*\*`([a-z_]+)`\*\*", section, flags=re.MULTILINE))
    # sin_confirmar va en su propio parrafo, antes de la lista: no es un estado que se le cuenta a nadie.
    assert written | {"sin_confirmar"} == set(RESOLVED_STATES)
    assert "Con `sin_confirmar`, volvé a leerla" in section


def test_the_meta_skill_reads_the_resolved_state_and_not_the_raw_one() -> None:
    section = _meta_skill_section("Cuando el dueño vuelve: cómo va")
    intro = section.split("- **`", 1)[0]
    assert "`resolved.state`" in intro and "No lo deduzcas de `status` ni de `list_templates`" in intro
    assert "traé la solicitud con `get_template_request`" in intro


@pytest.mark.parametrize("state", ["aprobada_sin_mandar", "no_llego_a_meta", "mandandose", "nombre_ocupado_en_meta",
                                   "no_llego_a_meta_sin_reintentos"])
def test_what_did_not_reach_meta_is_never_told_as_in_review_or_approved_by_it(state: str) -> None:
    line = _meta_skill_state(state)
    told = re.sub(r"(nunca|no) digas [^.]*\.", "", line, flags=re.IGNORECASE)
    assert "Sigue en revisión" not in told and "Meta la aprobó" not in told and "se aprobó" not in told


def test_the_branches_say_what_the_owner_has_to_hear() -> None:
    # Con la plantilla aprobada se destraba; nunca «sigue en revision».
    assert "«Destrabar el flujo»" in _meta_skill_state("aprobada_lista")
    assert "Nunca digas que sigue en revisión" in _meta_skill_state("aprobada_lista")
    assert "«Si Meta la cambió de tipo»" in _meta_skill_state("aprobada_otro_tipo")
    assert "«Si Meta la rechaza»" in _meta_skill_state("rechazada")
    assert "Sigue en revisión" in _meta_skill_state("en_revision")
    # Una persona la aprobo y Meta no la vio.
    person = _meta_skill_state("aprobada_sin_mandar")
    assert "todavía no se mandó a Meta" in person and "Volver a mandar" in person
    assert "nunca digas que la aprobó Meta ni que está en revisión" in person
    # El motivo sale del texto que da el sistema, no de lo que suponga el modelo.
    failed = _meta_skill_state("no_llego_a_meta")
    assert "`send_error_text`" in failed and "Volver a mandar" in failed and "no digas que está en revisión" in failed
    assert "No le agregues una causa tuya" in failed
    assert "No le agregues una causa tuya" in _meta_skill_section("Si Meta la rechaza")
    assert "No propongas otra plantilla" in person and "No calcules vos si venció" in person
    assert "Tampoco ofrezcas avisar" in _meta_skill_closing()
    assert "número siguiente" in _meta_skill_state("no_llego_a_meta_sin_reintentos")
    assert "número siguiente" in _meta_skill_state("nombre_ocupado_en_meta")
    assert "descartó" in _meta_skill_state("descartada")
    # Una pausada no se usa: no se la da por aprobada.
    assert "no se puede usar" in _meta_skill_state("pausada_en_meta")
    assert "No la des por aprobada" in _meta_skill_state("pausada_en_meta")
    assert "el flujo no la puede usar" in _meta_skill_state("aprobada_falta_catalogo")
    assert "No inventes un estado" in _meta_skill_state("estado_de_meta_desconocido")


def test_the_meta_skill_tells_the_result_where_it_really_is() -> None:
    closing = _meta_skill_closing()
    # La pagina se queda en «En revision» hasta la parte 4: no promete ver ahi el resultado.
    assert "te llega el aviso en este mismo hilo" in closing
    assert "página de la plantilla" not in _skill("flujos/pedir-plantilla-a-meta")
    contract = (ROOT / "docs" / "contracts" / "template-request-v1.md").read_text(encoding="utf-8")
    # Desde la parte 4 (E-361) el contrato dice que avisa el sistema, en el hilo donde se pidio.
    assert "avisa en el mismo hilo de Slack donde se pidió la plantilla" in contract


def test_the_meta_skill_does_not_send_a_missing_button_to_a_road_that_does_not_exist() -> None:
    closing = _meta_skill_closing()
    not_posted = closing.split("**`status: not_posted`**", 1)[1].split("\n", 1)[0]
    assert "nadie la puede mandar" in not_posted and "no la vuelvas a validar" in not_posted
    assert "operador" not in not_posted
    no_button = closing.split("**Sin `approval_button`**", 1)[1].split("\n", 1)[0]
    assert "no está prendido" in no_button and "equipo técnico" in no_button


def test_a_blocked_validation_redoes_the_same_request_and_moves_the_step_first() -> None:
    saving = _meta_skill_section("Cómo se guarda")
    blocked = saving.split("**Si vuelve con `blocks`** (sigue `borrador`)", 1)[1].split("\n", 1)[0]
    assert "mandando su `request_id` y su `revision`" in blocked and "Nunca una nueva" in blocked
    by_name = saving.split("**Si el bloqueo es por el nombre**", 1)[1].split("\n", 1)[0]
    # El orden importa: el validador compara con el paso que la espera (template_flow_step_mismatch).
    assert by_name.index("primero el paso del flujo") < by_name.index("después la plantilla")
    taken = next(line for line in _meta_skill_section("Errores, en simple").splitlines()
                 if line.startswith("- `meta_template_name_taken`"))
    assert "rehacelo con su `request_id` y su `revision`" in taken
    assert taken.index("primero el paso del flujo") < taken.index("después la plantilla")


@pytest.mark.parametrize("skill", ["flujos/crear-un-flujo", "flujos/pedir-plantilla-a-meta"])
def test_a_missing_button_detail_does_not_stop_the_flow(skill: str) -> None:
    assert ("pedilo una vez, pero no te frenes por eso: con el sí al resumen, guardá la propuesta con la plantilla "
            "pendiente y decí que ese dato va cuando se pida la plantilla a Meta") in _skill(skill)


def test_the_template_tools_are_in_the_soul_routing() -> None:
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    # La guia va antes que las herramientas: sin esa frase, el AE contestaba sin cargarla (suite E-356).
    assert "`pedir-plantilla-a-meta`" in soul and "nunca las uses sin antes cargar la guía" in soul
    assert len(soul) < 20_000


def test_the_soul_routes_any_question_about_a_template_to_the_guide_first() -> None:
    # Sin esta linea, el AE contestaba con solo el nombre de la herramienta (primera corrida de la suite de E-356).
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    routing = next(line for line in soul.splitlines() if line.startswith("- **Antes de contestar cualquier cosa sobre"))
    assert "una plantilla que no existe o que se pidió a Meta" in routing
    assert "cargá `pedir-plantilla-a-meta`" in routing and "aunque ya sepas qué herramienta llamar" in routing
    # Y va entre las guias, antes de lo que lee: se aplica antes de usar una herramienta.
    assert soul.index(routing) < soul.index("## Qué leés")


def test_the_tools_say_that_a_request_approved_by_a_person_did_not_reach_meta() -> None:
    schemas = (PROFILE / "plugins" / "automation-expert-change" / "schemas.py").read_text(encoding="utf-8")
    assert "«aprobada» ahí quiere decir que la aprobó una persona y NO se " in schemas
    assert "resolved.state: cómo va de verdad" in schemas
