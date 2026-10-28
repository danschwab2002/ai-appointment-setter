"""El boton «Revisar y aprobar» en el DM del Automation Expert (flow-change-cycle-v1 1.9.0).

«El recorrido», paso 1: cuando ``validate_flow_change`` deja la propuesta ``validada`` y sin
bloqueos, **este codigo**, no el modelo, postea en el mismo DM un mensaje fijo con un boton de URL
que abre ``https://<dominio del bridge>/flow-approval/p/<proposal_id>``.

- El mensaje es fijo: no lleva texto del modelo, ni el para que, ni ningun secreto. El link lleva
  solo el ``proposal_id`` (un UUID, que no da acceso). Sale con el unfurl apagado.
- El boton es un link: aprobar lo hace la pagina del bridge, con «Sign in with Slack». Este plugin
  no suma ninguna herramienta, y el profile sigue sin la clave de aprobar.
- Slack igual le avisa a la app cuando alguien aprieta un boton de URL. ``ack_only`` lo recibe y no
  hace nada mas, para que Slack no marque el boton con error (Hermes 0.21.0,
  ``register_slack_action_handler``, ``hermes_cli/plugins.py:3021``).
- Con el canal apagado (``FLOW_APPROVAL_ENABLED`` distinto de ``true``, el default) no postea
  nada y la respuesta de la herramienta queda igual que antes.

Lo lee del ``.env`` del profile: ``FLOW_APPROVAL_ENABLED``, ``FLOW_APPROVAL_BASE_URL`` (https, solo
el dominio publico del bridge, sin ruta) y el ``SLACK_BOT_TOKEN`` de la app del Automation Expert,
el mismo con el que Hermes contesta en ese DM. El DM y el hilo salen de la sesion de Hermes
(``HERMES_SESSION_CHAT_ID`` y ``HERMES_SESSION_THREAD_ID``).
"""

from __future__ import annotations

from collections.abc import Callable
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

ENABLED_SETTING = "FLOW_APPROVAL_ENABLED"
BASE_URL_SETTING = "FLOW_APPROVAL_BASE_URL"
BOT_TOKEN_SETTING = "SLACK_BOT_TOKEN"
ACTION_ID = "flow_approval_open"
BLOCK_ID = "flow_approval"
PAGE_PREFIX = "/flow-approval/p/"
SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
TIMEOUT_SECONDS = 10
BUTTON_TEXT = "Revisar y aprobar"
MESSAGE_TEXT = (
    "*La propuesta quedó validada.* La revisa y la aprueba una persona de la lista de aprobadores, "
    "en la página que abre el botón. El botón vale 24 horas."
)
FALLBACK_TEXT = "La propuesta quedó validada. Revisar y aprobar:"

# Lo que vuelve al agente: un estado de una palabra y una nota fija, nunca el link ni el error de Slack.
POSTED = {"status": "posted",
          "note": "El botón «Revisar y aprobar» ya quedó en este chat: el sí lo da un aprobador desde ahí."}
NOT_POSTED = {"status": "not_posted",
              "note": "El botón no salió. El sí lo da el operador, como hasta ahora."}

_DM_CHANNEL_RE = re.compile(r"D[A-Z0-9]{8,20}")
_THREAD_TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Rechaza toda redireccion: el token del bot nunca viaja a otro pedido."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def enabled(setting: Callable[[str], str]) -> bool:
    return setting(ENABLED_SETTING).strip().lower() == "true"


def approval_url(base_url: str, proposal_id: str) -> str:
    """El link de la pagina. Solo https, solo el dominio, y un UUID canonico al final."""
    base = base_url.strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("flow_approval_base_url_invalid")
    return f"{base}{PAGE_PREFIX}{UUID(proposal_id)}"


def build_message(*, channel: str, thread_ts: str | None, url: str) -> dict[str, object]:
    """El cuerpo de chat.postMessage: texto fijo y un boton de URL."""
    message: dict[str, object] = {
        "channel": channel,
        "text": f"{FALLBACK_TEXT} {url}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": MESSAGE_TEXT}},
            {"type": "actions", "block_id": BLOCK_ID, "elements": [{
                "type": "button",
                "action_id": ACTION_ID,
                "style": "primary",
                "text": {"type": "plain_text", "text": BUTTON_TEXT},
                "url": url,
            }]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return message


def _post(message: dict[str, object], token: str, endpoint: str) -> bool:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, method="POST", data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return False
    # Slack contesta 200 aunque rechace: lo que vale es ok en el cuerpo.
    return isinstance(payload, dict) and payload.get("ok") is True


def post_after_validation(
    proposal_id: str,
    *,
    setting: Callable[[str], str],
    session: Callable[[str], str],
    endpoint: str | None = None,
) -> dict[str, str] | None:
    """Postea el boton. None si el canal esta apagado: la herramienta contesta como antes."""
    if not enabled(setting):
        return None
    try:
        url = approval_url(setting(BASE_URL_SETTING), proposal_id)
    except ValueError:
        return dict(NOT_POSTED)
    channel = session("HERMES_SESSION_CHAT_ID").strip()
    token = setting(BOT_TOKEN_SETTING).strip()
    if (
        session("HERMES_SESSION_PLATFORM").strip().lower() != "slack"
        or not _DM_CHANNEL_RE.fullmatch(channel)
        or not token.startswith("xoxb-")
    ):
        return dict(NOT_POSTED)
    thread = session("HERMES_SESSION_THREAD_ID").strip()
    thread_ts = thread if _THREAD_TS_RE.fullmatch(thread) else None
    if not _post(build_message(channel=channel, thread_ts=thread_ts, url=url), token,
                 endpoint or SLACK_POST_MESSAGE):
        return dict(NOT_POSTED)
    return dict(POSTED)


async def ack_only(ack, body, action) -> None:
    """El clic en un boton de URL: Slack lo avisa igual. Se reconoce y nada mas."""
    del body, action
    await ack()
