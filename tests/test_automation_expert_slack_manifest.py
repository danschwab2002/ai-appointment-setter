"""El manifiesto de la app de Slack del Automation Expert (deploy/slack-app-manifest-automation-expert-v1.json).

Es el mismo para todas las instancias: quien instala lo pega en api.slack.com -> Create New App -> From
an app manifest. Los scopes y los eventos salen de la fuente de Hermes 0.21.0
(NousResearch/hermes-agent 29112be, la imagen de hermes/declaracion.toml), leida el 2026-10-23:
hermes_cli/slack_cli.py:79-160 (el manifiesto que genera `hermes slack manifest`) recortado a lo que
usa un profile de mensaje directo 1:1, y plugins/platforms/slack/adapter.py para cada metodo de la API
que llama (el detalle esta en docs/operations/automation-expert-slack-app-runbook.md).

Lo que estas pruebas cuidan es que el manifiesto y el profile digan lo mismo: el profile solo atiende
mensajes directos (config.yaml), asi que la app no pide leer canales, y Socket Mode queda prendido
porque cargar-automation-expert.py de la instancia prueba el app token con apps.connections.open.

Sin slash commands a proposito (M9, 2026-10-29): Slack le da cada comando a la ultima app que lo
instalo en todo el espacio de trabajo. Al instalar la app de Maribel en el Slack de Lancemos, se quedo
con el /model de otro agente de Hermes de ese espacio. Los comandos del profile se escriben en el DM
con "!" (!new, !stop, !status): el adaptador de Slack de Hermes los pasa a "/" si el nombre es un
comando conocido (plugins/platforms/slack/adapter.py, _rewrite_known_bang_command, :445-459), y los
controla el mismo user_allowed_commands.
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
    "files:write",       # files_upload_v2: el lienzo de draw_flow (MEDIA:) y cualquier adjunto
    "im:history",        # el evento message.im y conversations.replies dentro del DM
    "im:read",           # conversations.info de un canal D
    "im:write",          # conversations.open (_ensure_dm_conversation)
    "reactions:write",   # reactions.add/remove: los ojos mientras procesa (SLACK_REACTIONS, por defecto prendido)
    "users:read",        # users.info (_resolve_user_name)
}
EVENTOS = {"message.im"}
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
    assert "hermes-agent.local" not in json.dumps(m)


def test_los_scopes_son_los_de_un_profile_de_mensaje_directo() -> None:
    bot = _manifiesto()["oauth_config"]["scopes"]["bot"]
    assert len(bot) == len(set(bot))
    assert set(bot) == SCOPES
    assert not [s for s in bot if s.startswith(PROHIBIDOS)]
    # Sin slash commands no hace falta el scope que los habilita.
    assert "commands" not in bot
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


def test_no_registra_slash_commands() -> None:
    # Slack le da cada slash command a la ultima app que lo instalo en todo el espacio de trabajo: si
    # la app los registra, le saca /model (o /new, /stop...) a cualquier otro agente de Hermes del
    # mismo Slack.
    m = _manifiesto()
    assert "slash_commands" not in m["features"]
    assert "slash_commands" not in json.dumps(m)


def test_los_comandos_del_profile_se_escriben_con_signo_de_exclamacion() -> None:
    # Sin slash commands, los comandos que el profile deja a cualquiera llegan como texto del DM
    # ("!new", "!stop", "!status"): por eso el profile sigue declarandolos.
    permitidos = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["slack"]["user_allowed_commands"]
    assert set(permitidos) == {"new", "stop", "status"}
    assert set(_manifiesto()["settings"]["event_subscriptions"]["bot_events"]) == {"message.im"}


def test_el_nombre_es_igual_para_todas_las_instancias() -> None:
    m = _manifiesto()
    assert m["display_information"]["name"] == "Automation Expert"
    assert m["features"]["bot_user"]["display_name"] == "Automation Expert"
    texto = MANIFIESTO.read_text(encoding="utf-8").lower()
    for ajeno in ("att1", "johanna", "lancemos", "instancia", "<completar"):
        assert ajeno not in texto, ajeno
