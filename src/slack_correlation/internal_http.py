"""La unica excepcion al https del conector: http hacia un servicio declarado del stack.

Un conector por instancia corre en el mismo stack que su bridge y le habla por la red
interna (http://appointment-bridge:8000). Esa URL no tiene certificado, asi que el
https obligatorio la rechazaba. La excepcion es angosta a proposito, como la del
Automation Expert (AUTOMATION_EXPERT_INTERNAL_HOST):

- el host:puerto tiene que estar declarado por variable, exacto, y el texto de la URL
  tiene que ser exactamente http://<ese host:puerto> (con o sin la barra final);
- el host es un nombre de servicio de una sola etiqueta (sin puntos), que no sea un
  numero: no entra un dominio publico ni una IP;
- el puerto es explicito.

Es un control compensatorio, no la prueba de que el trafico no sale del host: el
nombre lo resuelve el DNS del contenedor. Sin la variable, todo sigue como antes.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_SERVICE_HOST = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def validate_internal_host(value: str | None, *, error: str) -> str | None:
    """Devuelve el host:puerto declarado, None si no hay, o ValueError(error)."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    host, separator, port_text = normalized.rpartition(":")
    if (
        not separator
        or _SERVICE_HOST.fullmatch(host) is None
        or host.replace("-", "").replace("_", "").isdigit()
        or not port_text.isdigit()
        or port_text != str(int(port_text))
        or not 1 <= int(port_text) <= 65535
    ):
        raise ValueError(error)
    return normalized


def is_declared_internal_http(base_url: str, internal_host: str | None) -> bool:
    """True solo si base_url es exactamente http://<internal_host>[/]."""

    if not internal_host or not isinstance(base_url, str):
        return False
    return base_url in (f"http://{internal_host}", f"http://{internal_host}/") and (
        urlsplit(base_url).netloc == internal_host
    )
