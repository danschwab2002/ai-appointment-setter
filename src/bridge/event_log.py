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

# Las lineas info que salen al log. Del webhook de Hotmart y de su worker: lo que
# Hotmart manda y el bridge no usa, o lo que hizo con un evento. Y la pausa al
# comprar: ids de evento, accion y conversacion, asignacion y motivo.
LOGGED_INFO_EVENTS = frozenset(
    {
        # el webhook (bridge.app)
        "hotmart_event_ignored",
        "hotmart_event_filtered",
        "hotmart_event_unrouted",
        "hotmart_event_rejected",
        "hotmart_listen_only_capture_skipped",
        "hotmart_listen_only_captured",
        "hotmart_event_late",
        "hotmart_payment_slip_admitted",
        # el worker (bridge.worker)
        "hotmart_event_not_planned",
        "hotmart_purchase_processed",
        # la pausa al comprar (bridge.purchase_agent_pause y bridge.worker, E-283)
        "purchase_agent_paused",
        "purchase_agent_pause_skipped",
        "purchase_agent_paused_after_welcome",
        "purchase_agent_pause_after_welcome_skipped",
        # los rechazos de Meta (bridge.meta_delivery, E-284): codigo, tipo de
        # toque, plantilla y numero de conversacion o de mensaje; nunca el
        # telefono ni el texto
        "meta_delivery_scan",
        "meta_delivery_rejected",
        "meta_first_touch_retry",
        "meta_first_touch_retry_skipped",
        "meta_delivery_notice_already_admitted",
        # si la persona ya es cliente (bridge.app, E-297): cuantos productos,
        # sus estados y fuentes, y el numero de conversacion; nunca el
        # telefono ni el mail
        "customer_status_context",
        "customer_status_link_blocked",
        "customer_status_coupon_skipped",
        # el aviso al derivar a una clienta (bridge.app, E-304): si salio o
        # por que no, el motivo de la derivacion y el numero de conversacion
        "customer_status_handoff_notice",
        # el mensaje de seguridad al derivar por riesgo (bridge.app, E-328):
        # si salio o por que no y el numero de conversacion; nunca el texto
        "safety_handoff_reply",
        # el aviso de derivaciones sin atender (bridge.handoff_wait_alerts,
        # E-309): conteos del barrido, y por aviso el tipo, el numero de
        # conversacion, el motivo y las horas de espera; nunca el telefono
        "handoff_wait_scan",
        "handoff_wait_alert_admitted",
        # los segmentos por etiqueta (bridge.app, E-311): clave, version, sha
        # corto y numero de conversacion de cada segmento que fue, y por que no
        # fue uno etiquetado (vencido o por empezar). Los recortes por tope son
        # warning y salen siempre.
        "segment_context",
        "segment_inactive",
        # la etiqueta que pone la palabra clave del primer mensaje
        # (bridge.campaign_inbound_entry, E-317; E-336): numero de conversacion,
        # clave y etiqueta del segmento y lo que hizo el macro; nunca el texto
        # del mensaje, el telefono ni el nombre
        "segment_label_by_keyword",
        # el adjunto sin texto (bridge.app, E-315): el id del mensaje, los
        # tipos de adjunto y si fue el segundo seguido; nunca el archivo
        "chatwoot_inbound_attachment_without_text",
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
