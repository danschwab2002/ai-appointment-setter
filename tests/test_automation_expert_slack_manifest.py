"""El manifiesto de la app de Slack del Automation Expert (deploy/slack-app-manifest-automation-expert-v1.json).

Es el mismo para todas las instancias: quien instala lo pega en api.slack.com -> Create New App -> From
an app manifest. Los scopes, los eventos y los slash commands salen de la fuente de Hermes 0.21.0
(NousResearch/hermes-agent 29112be, la imagen de hermes/declaracion.toml), leida el 2026-10-23:
hermes_cli/slack_cli.py:79-160 (el manifiesto que genera `hermes slack manifest`) recortado a lo que
usa un profile de mensaje directo 1:1, y plugins/platforms/slack/adapter.py para cada metodo de la API
que llama (el detalle esta en docs/operations/automation-expert-slack-app-runbook.md).

Lo que estas pruebas cuidan es que el manifiesto y el profile digan lo mismo: el profile solo atiende
mensajes directos (config.yaml), asi que la app no pide leer canales, y Socket Mode queda prendido
porque cargar-automation-expert.py de la instancia prueba el app token con apps.connections.open.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFIESTO = ROOT / "deploy" / "slack-app-manifest-automation-expert-v1.json"
CONFIG = ROOT / "profiles" / "automation-expert" / "config.yaml"

SCOPES = {
    "chat:write",        # chat.postMessage, chat.update, chat.delete, chat.postEphemeral, los streams
    "commands",          # los slash commands de abajo
    "files:write",       # files_upload_v2: el lienzo de draw_flow (MEDIA:) y cualquier adjunto
    "im:history",        # el evento message.im y conversations.replies dentro del DM
    "im:read",           # conversations.info de un canal D
    "im:write",          # conversations.open (_ensure_dm_conversation)
    "reactions:write",   # reactions.add/remove: los ojos mientras procesa (SLACK_REACTIONS, por defecto prendido)
    "users:read",        # users.info (_resolve_user_name)
}
EVENTOS = {"message.im"}
SLASHES = {"/hermes", "/new", "/stop", "/model", "/restart"}
# Lo que el profile no usa: canales, DMs de grupo, archivos ajenos y la vista de asistente.
PROHIBIDOS = ("channels:", "groups:", "mpim:", "app_mentions:", "files:read", "assistant:", "incoming-webhook")


def _manifiesto() -> dict:
    return json.loads(MANIFIESTO.read_text(encoding="utf-8"))


def test_el_manifiesto_es_socket_mode_sin_urls_publicas() -> None:
    m = _manifiesto()
    ajustes = m["settings"]
    assert ajustes["socket_mode_enabled"] is True
    assert ajustes["token_rotation_enabled"] is False
    assert ajustes["org_deploy_enabled"] is False
    assert "request_url" not in json.dumps(ajustes)
    # En Socket Mode Slack ignora la url de cada slash command (hermes_cli/commands.py:1580-1582):
    # va el marcador de Hermes, nunca un host de una instancia.
    assert {c["url"] for c in m["features"]["slash_commands"]} == {"https://hermes-agent.local/slack/commands"}


def test_los_scopes_son_los_de_un_profile_de_mensaje_directo() -> None:
    bot = _manifiesto()["oauth_config"]["scopes"]["bot"]
    assert len(bot) == len(set(bot))
    assert set(bot) == SCOPES
    assert not [s for s in bot if s.startswith(PROHIBIDOS)]
    assert "user" not in _manifiesto()["oauth_config"]["scopes"]


def test_solo_escucha_mensajes_directos() -> None:
    m = _manifiesto()
    assert set(m["settings"]["event_subscriptions"]["bot_events"]) == EVENTOS
    pestanas = m["features"]["app_home"]
    # Sin la pestana de mensajes Slack no deja escribirle al bot.
    assert pestanas["messages_tab_enabled"] is True
    assert pestanas["messages_tab_read_only_enabled"] is False


def test_el_profile_atiende_solo_mensajes_directos() -> None:
    slack = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["slack"]
    # Un centinela que no es un canal real deja afuera los canales: el manifiesto no pide leerlos.
    assert slack["allowed_channels"] == ["CNONE000000"]
    assert slack["allow_bots"] == "none"


def test_los_slash_commands_cubren_los_comandos_del_profile() -> None:
    m = _manifiesto()
    nombres = {c["command"] for c in m["features"]["slash_commands"]}
    assert nombres == SLASHES
    assert all(c["should_escape"] is False for c in m["features"]["slash_commands"])
    permitidos = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["slack"]["user_allowed_commands"]
    # /status choca con un comando propio de Slack: Hermes no lo registra y se llega por /hermes status
    # (hermes_cli/commands.py:1508-1510).
    for comando in permitidos:
        assert f"/{comando}" in nombres or comando == "status", comando


def test_el_nombre_es_igual_para_todas_las_instancias() -> None:
    m = _manifiesto()
    assert m["display_information"]["name"] == "Automation Expert"
    assert m["features"]["bot_user"]["display_name"] == "Automation Expert"
    texto = MANIFIESTO.read_text(encoding="utf-8").lower()
    for ajeno in ("att1", "johanna", "lancemos", "instancia", "<completar"):
        assert ajeno not in texto, ajeno
