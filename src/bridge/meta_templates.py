"""El cliente cerrado de la API de plantillas de Meta (template-request-v1, F-146 parte 3).

Es todo lo que el bridge le pide a Meta, y son tres llamadas:

* ``create``: ``POST /{version}/{waba}/message_templates``. El unico verbo de
  escritura. Crea una plantilla; no edita ni borra.
* ``find``: ``GET /{version}/{waba}/message_templates``, para saber si un nombre
  con su idioma ya existe. Es la idempotencia del envio: un reintento encuentra
  lo que el anterior llego a crear y no crea otra.
* ``read``: ``GET /{version}/{template_id}``, el estado de una plantilla (lo usa
  el sondeo de la parte 4).

Nada sobre el numero, los webhooks ni la suscripcion, aunque el permiso
``whatsapp_business_management`` los alcance. Una prueba mira el modulo y falla
si aparece otra ruta u otro verbo (tests/test_meta_templates.py).

**El token** va en el header, nunca en la URL, y nada de lo que devuelve Meta
sale de este modulo como texto: un error es solo su codigo numerico
(``meta:190``, ``meta:100:2388040``), que es lo que se guarda y lo que la pagina
traduce. ``paging.next`` trae una URL armada por Meta: no se sigue, se pide la
pagina siguiente con el cursor.

**La forma de las respuestas.** La lista y la plantilla son las que Chatwoot
copia de Meta al catalogo (capturas en ``tests/fixtures/``). Crear y los errores
siguen la documentacion publica de Meta, sin captura: ver
``tests/fixtures/meta_graph_templates_documented_20261108.json``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "DEFAULT_GRAPH_VERSION",
    "MetaTemplate",
    "MetaTemplatesClient",
    "MetaTemplatesError",
    "body_of",
    "content_of",
]

GRAPH_ORIGIN = "https://graph.facebook.com"
# La que verifico meta_salud.py contra Meta el 2026-10-30.
DEFAULT_GRAPH_VERSION = "v23.0"
PAGE_SIZE = 100
# 6.000 plantillas es el tope de Meta para un portfolio verificado (250 sin verificar).
MAX_PAGES = 70
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FIELDS = "id,name,language,status,category,components"
_TEMPLATE_ID = re.compile(r"^[0-9]{1,32}$")
_WABA_ID = re.compile(r"^[0-9]{6,32}$")
_GRAPH_VERSION = re.compile(r"^v[0-9]{1,3}\.[0-9]{1,2}$")


class MetaTemplatesError(Exception):
    """Meta no hizo lo pedido. ``code`` es un codigo cerrado: ningun texto de Meta viaja con el."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MetaTemplate:
    """Lo que el bridge usa de una plantilla de Meta. ``body`` es None si la respuesta no trae los componentes."""

    template_id: str
    status: str
    category: str | None = None
    name: str | None = None
    language: str | None = None
    body: str | None = None
    rejected_reason: str | None = None
    content: tuple[Any, ...] | None = None


def body_of(components: object) -> str | None:
    """El texto del componente ``BODY`` de un pedido o de una plantilla de Meta."""
    if not isinstance(components, list):
        return None
    for component in components:
        if isinstance(component, Mapping) and str(component.get("type", "")).upper() == "BODY":
            text = component.get("text")
            return text if isinstance(text, str) else None
    return None


def content_of(components: object) -> tuple[Any, ...] | None:
    """Lo que la persona ve de una plantilla: encabezado, cuerpo, pie y botones. None si no se puede leer.

    Los ejemplos no cuentan: son para la revision de Meta, no para la persona.
    """
    if not isinstance(components, list):
        return None
    header: tuple[str, object] | None = None
    body: object = None
    footer: object = None
    buttons: tuple[tuple[str, object, object], ...] = ()
    for component in components:
        if not isinstance(component, Mapping):
            return None
        kind = str(component.get("type", "")).upper()
        if kind == "HEADER":
            header = (str(component.get("format", "")).upper(), component.get("text"))
        elif kind == "BODY":
            body = component.get("text")
        elif kind == "FOOTER":
            footer = component.get("text")
        elif kind == "BUTTONS":
            raw = component.get("buttons")
            if not isinstance(raw, list) or not all(isinstance(button, Mapping) for button in raw):
                return None
            buttons = tuple((str(button.get("type", "")).upper(), button.get("text"), button.get("url"))
                            for button in raw)
    return (header, body, footer, buttons) if isinstance(body, str) else None


def _template(raw: object) -> MetaTemplate | None:
    if not isinstance(raw, Mapping):
        return None
    template_id, status = raw.get("id"), raw.get("status")
    if not isinstance(template_id, str) or not _TEMPLATE_ID.fullmatch(template_id) or not isinstance(status, str):
        return None
    category, name, language, reason = (raw.get("category"), raw.get("name"), raw.get("language"),
                                        raw.get("rejected_reason"))
    return MetaTemplate(template_id=template_id, status=status.upper(),
                        category=category.upper() if isinstance(category, str) else None,
                        name=name if isinstance(name, str) else None,
                        language=language if isinstance(language, str) else None,
                        body=body_of(raw.get("components")),
                        content=content_of(raw.get("components")),
                        rejected_reason=reason if isinstance(reason, str) else None)


def _error_code(response: httpx.Response) -> str:
    """El codigo de un error de Meta. ``error_subcode`` Meta lo da por deprecado, pero en plantillas lo manda."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    if type(code) is int and code >= 0:
        subcode = error.get("error_subcode")
        return f"meta:{code}:{subcode}" if type(subcode) is int and subcode >= 0 else f"meta:{code}"
    return f"meta_http:{response.status_code}"


class MetaTemplatesClient:
    """Las tres llamadas. Lo crea el bridge solo con ``META_TEMPLATES_ENABLED``."""

    def __init__(self, *, access_token: str, waba_id: str, graph_version: str = DEFAULT_GRAPH_VERSION,
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0) -> None:
        if not access_token or any(character.isspace() for character in access_token):
            raise ValueError("meta_templates_access_token_invalid")
        if not _WABA_ID.fullmatch(waba_id):
            raise ValueError("meta_templates_waba_invalid")
        if not _GRAPH_VERSION.fullmatch(graph_version):
            raise ValueError("meta_templates_graph_version_invalid")
        self._access_token = access_token
        self._waba_id = waba_id
        self._graph_version = graph_version
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"MetaTemplatesClient(graph_version={self._graph_version!r})"

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=GRAPH_ORIGIN, transport=self._transport, timeout=self._timeout_seconds,
                                 headers={"Authorization": f"Bearer {self._access_token}"},
                                 follow_redirects=False)

    @staticmethod
    def _parsed(response: httpx.Response) -> Mapping[str, Any]:
        if response.status_code != 200:
            raise MetaTemplatesError(_error_code(response))
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise MetaTemplatesError("meta_unexpected_answer")
        try:
            body = response.json()
        except ValueError:
            raise MetaTemplatesError("meta_unexpected_answer") from None
        if not isinstance(body, Mapping):
            raise MetaTemplatesError("meta_unexpected_answer")
        return body

    async def create(self, request: Mapping[str, Any]) -> MetaTemplate:
        """Crea la plantilla. El unico POST del modulo.

        Sin respuesta (o con un 5xx) no se sabe si Meta la creo: lo resuelve el
        reintento, que antes de crear busca el nombre.
        """
        path = f"/{self._graph_version}/{self._waba_id}/message_templates"
        try:
            async with self._http() as client:
                response = await client.post(path, content=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
        except httpx.HTTPError:
            raise MetaTemplatesError("meta_unreachable") from None
        created = _template(self._parsed(response))
        if created is None:
            raise MetaTemplatesError("meta_unexpected_answer")
        return created

    async def find(self, name: str, language: str) -> MetaTemplate | None:
        """La plantilla con ese nombre e idioma exactos, o None si la cuenta no la tiene.

        Recorre la lista entera y filtra aca: la referencia de Meta no documenta
        un filtro por nombre en esta ruta. Si la lista no se puede leer hasta el
        final no se afirma que no existe: sale con error, y no se crea nada.
        """
        path = f"/{self._graph_version}/{self._waba_id}/message_templates"
        params: dict[str, str | int] = {"fields": FIELDS, "limit": PAGE_SIZE}
        try:
            async with self._http() as client:
                for _ in range(MAX_PAGES):
                    page = self._parsed(await client.get(path, params=params))
                    rows = page.get("data")
                    if not isinstance(rows, list):
                        raise MetaTemplatesError("meta_unexpected_answer")
                    for row in rows:
                        if isinstance(row, Mapping) and row.get("name") == name and row.get("language") == language:
                            found = _template(row)
                            if found is None:
                                raise MetaTemplatesError("meta_unexpected_answer")
                            return found
                    paging = page.get("paging")
                    if not isinstance(paging, Mapping) or not paging.get("next"):
                        return None
                    cursors = paging.get("cursors")
                    after = cursors.get("after") if isinstance(cursors, Mapping) else None
                    if not isinstance(after, str) or not after:
                        raise MetaTemplatesError("meta_unexpected_answer")
                    params = {**params, "after": after}
        except httpx.HTTPError:
            raise MetaTemplatesError("meta_unreachable") from None
        raise MetaTemplatesError("meta_list_too_long")

    async def read(self, template_id: str) -> MetaTemplate:
        """El estado de una plantilla por su id. Solo lee."""
        if not _TEMPLATE_ID.fullmatch(template_id):
            raise MetaTemplatesError("meta_template_id_invalid")
        try:
            async with self._http() as client:
                response = await client.get(f"/{self._graph_version}/{template_id}",
                                            params={"fields": "id,status,category,rejected_reason"})
        except httpx.HTTPError:
            raise MetaTemplatesError("meta_unreachable") from None
        found = _template(self._parsed(response))
        if found is None:
            raise MetaTemplatesError("meta_unexpected_answer")
        return found
