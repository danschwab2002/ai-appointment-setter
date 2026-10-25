"""Que las lineas ``info`` revisadas del bridge lleguen al log del contenedor.

El bridge corre bajo uvicorn, que configura solo sus propios loggers. El logger
raiz queda sin handler, asi que un ``logger.warning`` del bridge sale por el
handler de ultimo recurso de ``logging`` (stderr, solo el mensaje) y un
``logger.info`` se pierde. Por eso ``hotmart_event_filtered`` (E-057) existia en
el codigo y no en produccion (E-081).

Esto pone un handler en el logger ``bridge`` con el mismo formato que el de
ultimo recurso (solo el mensaje), asi los warnings salen igual que antes, una
sola vez. De los ``info`` pasan solamente los de ``LOGGED_INFO_EVENTS``: cada uno
se reviso y lleva ids, tipos y motivos, nunca mail, telefono ni nombre. Un
``info`` nuevo que se quiera ver en produccion se agrega a la lista despues de
revisar que no lleve datos personales; los demas siguen sin salir.

Si el logger raiz ya tiene handlers (pytest, o un ``--log-config`` que lo
configure) no se instala nada: ahi los registros ya tienen salida y un handler
mas duplicaria las lineas.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

BRIDGE_LOGGER = "bridge"

# Las lineas info que salen al log. Todas del webhook de Hotmart y de su worker:
# lo que Hotmart manda y el bridge no usa, o lo que hizo con un evento.
LOGGED_INFO_EVENTS = frozenset(
    {
        # el webhook (bridge.app)
        "hotmart_event_ignored",
        "hotmart_event_filtered",
        "hotmart_event_unrouted",
        "hotmart_event_rejected",
        "hotmart_listen_only_capture_skipped",
        "hotmart_listen_only_captured",
        # el worker (bridge.worker)
        "hotmart_event_not_planned",
        "hotmart_purchase_processed",
    }
)


def _event_name(record: logging.LogRecord) -> str:
    message = record.msg if isinstance(record.msg, str) else ""
    return message.split(" ", 1)[0]


class _ReviewedInfoOnly(logging.Filter):
    """Deja pasar todo warning o mas, y de los info solo los revisados."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.levelno == logging.INFO and (
            _event_name(record) in LOGGED_INFO_EVENTS
        )


class _BridgeEventHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marca para no instalar el handler dos veces."""


def configure_bridge_event_log(stream: TextIO | None = None) -> bool:
    """Instala el handler del logger ``bridge``. Devuelve si lo instalo ahora.

    Idempotente: ``create_app`` lo llama en cada armado de la app.
    """
    bridge_logger = logging.getLogger(BRIDGE_LOGGER)
    if any(isinstance(h, _BridgeEventHandler) for h in bridge_logger.handlers):
        return False
    if logging.getLogger().handlers:
        return False
    handler = _BridgeEventHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_ReviewedInfoOnly())
    bridge_logger.addHandler(handler)
    if bridge_logger.level == logging.NOTSET or bridge_logger.level > logging.INFO:
        bridge_logger.setLevel(logging.INFO)
    return True


_HOTMART_EVENT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_HOTMART_EVENT_TYPE = re.compile(r"[A-Z0-9_]{1,64}")


def _shaped(value: object, shape: re.Pattern[str]) -> str:
    if value is None:
        return "-"
    if isinstance(value, str) and shape.fullmatch(value):
        return value
    return "<otro>"


def hotmart_log_fields(event_id: object, event_type: object) -> tuple[str, str]:
    """El id y el tipo de un evento para el log, solo si tienen la forma de
    Hotmart; si no, ``<otro>``: nunca texto libre del payload."""
    return _shaped(event_id, _HOTMART_EVENT_ID), _shaped(event_type, _HOTMART_EVENT_TYPE)
