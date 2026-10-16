"""La etiqueta de producto en Chatwoot (varios productos por aliada, paso 2a).

El bridge le suma a cada conversacion la etiqueta del producto que declara
``[[productos]]`` en el manifiesto, para que el equipo vea en el inbox de que
producto es cada conversacion: cuando la aliada tenga dos productos, el mismo
numero de WhatsApp atiende a los dos. Se pone cuando la persona escribe y cuando
una plantilla abre la conversacion.

La etiqueta se suma ejecutando un macro que solo hace ``add_label`` con ella. El
endpoint de etiquetas de la conversacion reemplaza la lista entera y podria borrar
``automation_paused``; la accion ``add_label`` de un macro suma (en Chatwoot,
``ActionService#add_label`` llama a ``add_labels``).

Falla blanda, siempre: una etiqueta que no sale no frena la respuesta al lead ni
el envio de una plantilla. Se ve en el log y en ``/ready``.

Antes de la primera ejecucion se lee el macro. Si hace algo mas que sumar esta
etiqueta (por ejemplo, porque se cargo el id del macro de pausa), el etiquetado
queda apagado hasta el proximo arranque, con un error en el log: un macro
equivocado ejecutado sobre cada conversacion del inbox es peor que no etiquetar.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Protocol

import httpx

from .chatwoot import ChatwootProtocolError

logger = logging.getLogger(__name__)

# Cuantas conversaciones ya etiquetadas se recuerdan, para no leer Chatwoot en cada
# mensaje. Al reiniciar se olvidan, y la primera lectura las vuelve a encontrar.
DEFAULT_REMEMBERED_CONVERSATIONS = 4096


class LabelClient(Protocol):
    async def get_macro_actions(self, *, macro_id: int) -> list[dict[str, object]]: ...

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]: ...

    async def execute_macro(self, *, macro_id: int, conversation_id: int) -> None: ...


class ProductLabeler:
    """Suma la etiqueta de un producto a las conversaciones, por su macro."""

    def __init__(
        self,
        *,
        client: LabelClient,
        label: str,
        macro_id: int,
        remembered: int = DEFAULT_REMEMBERED_CONVERSATIONS,
    ) -> None:
        if not label or type(macro_id) is not int or macro_id < 1 or remembered < 1:
            raise ValueError("invalid product label configuration")
        self._client = client
        self._label = label
        self._macro_id = macro_id
        self._remembered = remembered
        self._labeled: OrderedDict[int, None] = OrderedDict()
        # unverified: todavia no se leyo el macro. ready: el macro solo suma la
        # etiqueta. disabled:<motivo>: el macro no sirve y no se ejecuta mas.
        self._state = "unverified"

    @property
    def label(self) -> str:
        return self._label

    @property
    def state(self) -> str:
        return self._state

    async def ensure(self, conversation_id: int, *, source: str) -> str:
        """Suma la etiqueta si falta. Nunca lanza: devuelve que paso.

        * ``present``: ya estaba.
        * ``added``: se ejecuto el macro. Chatwoot lo aplica en un job; la
          proxima vez que pase la conversacion se confirma leyendo.
        * ``disabled``: el macro no es valido y el etiquetado esta apagado.
        * ``failed``: fallo Chatwoot o la conversacion no es valida; la proxima
          vez se reintenta.
        """

        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id < 1
        ):
            logger.warning(
                "product_label_failed source=%s reason=invalid_conversation_id",
                source,
            )
            return "failed"
        if self._state.startswith("disabled"):
            return "disabled"
        if conversation_id in self._labeled:
            self._labeled.move_to_end(conversation_id)
            return "present"
        try:
            if self._state == "unverified":
                problem = await self._macro_problem()
                if problem is not None:
                    self._state = f"disabled:{problem}"
                    logger.error(
                        "product_label_macro_invalid macro=%s label=%s reason=%s",
                        self._macro_id,
                        self._label,
                        problem,
                    )
                    return "disabled"
                self._state = "ready"
            labels = await self._client.get_conversation_labels(
                conversation_id=conversation_id
            )
            if self._label in labels:
                self._remember(conversation_id)
                outcome = "present"
            else:
                await self._client.execute_macro(
                    macro_id=self._macro_id,
                    conversation_id=conversation_id,
                )
                outcome = "added"
        except Exception as exc:
            logger.warning(
                "product_label_failed source=%s conversation=%s reason=%s",
                source,
                conversation_id,
                _reason(exc),
            )
            return "failed"
        logger.info(
            "product_label outcome=%s source=%s conversation=%s label=%s",
            outcome,
            source,
            conversation_id,
            self._label,
        )
        return outcome

    async def _macro_problem(self) -> str | None:
        try:
            actions = await self._client.get_macro_actions(macro_id=self._macro_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "macro_not_found"
            raise
        if actions != [{"action_name": "add_label", "action_params": [self._label]}]:
            return "macro_does_more_than_add_the_label"
        return None

    def _remember(self, conversation_id: int) -> None:
        self._labeled[conversation_id] = None
        self._labeled.move_to_end(conversation_id)
        while len(self._labeled) > self._remembered:
            self._labeled.popitem(last=False)


def _reason(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, ChatwootProtocolError):
        return str(exc) or "chatwoot_protocol_error"
    if isinstance(exc, httpx.HTTPError):
        return "chatwoot_http_error"
    return type(exc).__name__
