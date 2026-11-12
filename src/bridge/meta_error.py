"""El codigo de Meta de un mensaje que no salio, en cualquiera de sus formas (E-486).

Chatwoot guarda el rechazo de WhatsApp en ``content_attributes.external_error``.
El bridge lo leia solo como «131049: Message failed to send…». En la QA del
12/11 una parte de los ``failed`` venian con el error como JSON adentro de un
string (``'{"code": 131050, "title": "…"}'``, a veces envuelto en ``error`` o en
una lista), y el lector los contaba sin codigo. Esta funcion saca solo los
digitos del codigo, nunca el texto: el texto de Meta puede traer el telefono.
"""

from __future__ import annotations

import json
import re

_LEADING_CODE = re.compile(r"^(\d{3,6})(?:\D|$)")
_CODE = re.compile(r"^\d{3,6}$")
_MAX_DEPTH = 4


def meta_error_code_of(value: object, _depth: int = 0) -> str | None:
    """Los digitos del codigo de Meta, o None si no hay uno legible."""
    if _depth > _MAX_DEPTH or value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
        return text if _CODE.fullmatch(text) else None
    if isinstance(value, str):
        stripped = value.strip()
        match = _LEADING_CODE.match(stripped)
        if match:
            return match.group(1)
        if stripped[:1] in ("{", "[", '"'):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                return None
            return meta_error_code_of(parsed, _depth + 1)
        return None
    if isinstance(value, dict):
        for key in ("code", "error_code"):
            if key in value:
                code = meta_error_code_of(value[key], _depth + 1)
                if code is not None:
                    return code
        for key in ("error", "errors", "external_error"):
            if key in value:
                code = meta_error_code_of(value[key], _depth + 1)
                if code is not None:
                    return code
        return None
    if isinstance(value, list) and value:
        return meta_error_code_of(value[0], _depth + 1)
    return None
