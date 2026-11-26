"""La instruccion temporal para todas las conversaciones (agent-temporary-instruction-v1, 5.3 de Z1).

Contrato: ``docs/contracts/agent-temporary-instruction-v1.md``. Pedido: «hoy no
ofrezcas cupon», «avisa que el vivo se movio», sin un PR, una firma ni un
redeploy. Decisiones de partida D4 y D11 de la serie Z1: la redacta el AE con el
duenio, la aprueba una persona de ``aprobadores_slack`` en la pagina; una sola
viva por instancia, hasta 600 caracteres, ``hasta`` obligatorio y como mucho 7
dias; sin links, telefonos, mails ni montos.

Este modulo es la parte pura y el sosten:

* ``parse_instruction`` y ``instruction_blocks``: las reglas (las mismas en la
  vista previa, al proponer y al volver a leer la fila publicada).
* ``render``: el bloque que lee el agente, con el preambulo fijo del producto.
* ``TemporaryInstructionBlock``: un ``system`` extra de Hermes
  (``bridge.hermes.ExtraSystemBlock``), con ``kind`` para la procedencia.
* ``LiveTemporaryInstruction``: la publicada de la base, con cache corta y lo
  ultimo bueno si la base no contesta. El ``hasta`` se mira en cada turno: al
  vencer deja de servirse sola, tambien sin base.

Los logs llevan el id, la version y el motivo; nunca el texto.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .flow_change_api import looks_like_identifier
from .price_guard import extract_amounts

__all__ = [
    "CONTRACT",
    "KIND",
    "MAX_CHARS",
    "MAX_DAYS",
    "RULES_VERSION",
    "InstructionDraft",
    "InstructionInvalid",
    "LiveTemporaryInstruction",
    "TemporaryInstructionBlock",
    "instruction_blocks",
    "parse_instruction",
    "render",
]

logger = logging.getLogger(__name__)

CONTRACT = "agent-temporary-instruction-v1"
RULES_VERSION = 1
FLAG_ENV = "AGENT_TEMPORARY_INSTRUCTION_ENABLED"
# El tipo del bloque en la procedencia del turno (D-53.9): no es un segmento.
KIND = "instruccion_temporal"
MAX_CHARS = 600
MAX_DAYS = 7
REFRESH_SECONDS = 30.0
HEADING = "# Instrucción temporal del equipo"

# Lo que rechaza la base (``_agent_temporary_instruction_text_is_valid``): los
# controles salvo el salto de linea y los invisibles del texto libre del almacen.
_INVISIBLE = re.compile(
    "[\u0001-\u0009\u000b-\u001f\u007f-\u009f­؜᠎​-‏ -‮"
    "⁠-⁤⁦-⁯﻿￹-￻\U000e0000-\U000e007f]"
)
# Un link: con esquema, con www., wa.me o un dominio con una terminacion conocida (D-53.8).
_TLDS = (
    "com|net|org|info|biz|io|co|me|ly|app|dev|ai|site|online|store|shop|link|page|live|tv|gg|to|xyz|club|"
    "academy|edu|gov|mx|ar|cl|pe|uy|es|br|us|ec|bo|py|ve|gt|cr|pa|do|hn|sv|ni|cu|pr|ca|uk|de|fr|it|pt"
)
_LINK = re.compile(
    r"(?i)(?:https?://|www\.|\bwa\.me/|(?<![\w@.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + _TLDS + r")(?![a-z0-9-]))"
)


class InstructionInvalid(ValueError):
    """Lo que mando el agente no sirve. ``reason`` lleva nombres de campos, nunca valores."""

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


@dataclass(frozen=True)
class InstructionDraft:
    """El texto normalizado y su ``hasta``."""

    text: str
    valid_until: datetime

    @property
    def valid_until_iso(self) -> str:
        return self.valid_until.isoformat(timespec="seconds").replace("+00:00", "Z")


def _refused(field: str, problem: str) -> InstructionInvalid:
    return InstructionInvalid("temporary_instruction_invalid", {"field": field, "problem": problem})


def parse_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _refused("texto", "not_a_text")
    text = value.strip().replace("\r\n", "\n")
    if len(text) > MAX_CHARS:
        raise _refused("texto", f"longer_than_{MAX_CHARS}")
    if _INVISIBLE.search(text):
        raise _refused("texto", "control_characters")
    return text


def parse_until(value: object) -> datetime:
    if not isinstance(value, str):
        raise _refused("hasta", "not_a_date_with_zone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _refused("hasta", "not_a_date_with_zone") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _refused("hasta", "not_a_date_with_zone")
    return parsed.astimezone(UTC).replace(microsecond=0)


def parse_instruction(raw: object) -> InstructionDraft:
    """Lo que manda el agente: ``{"texto": ..., "hasta": ...}``. Lo que no cierra se rechaza sin el valor."""
    if not isinstance(raw, Mapping):
        raise _refused("instruction", "not_an_object")
    keys = set(raw)
    if keys != {"texto", "hasta"}:
        reason: dict[str, Any] = {"field": "instruction"}
        if missing := sorted({"texto", "hasta"} - keys):
            reason["missing"] = missing
        if extra := keys - {"texto", "hasta"}:
            reason["extra"] = sorted(key if isinstance(key, str) and key.isascii() and len(key) <= 40 else "<otra>"
                                     for key in extra)[:10]
        raise InstructionInvalid("temporary_instruction_invalid", reason)
    return InstructionDraft(text=parse_text(raw["texto"]), valid_until=parse_until(raw["hasta"]))


def _finding(code: str, field: str) -> dict[str, Any]:
    return {"code": code, "field": field}


def text_blocks(text: str) -> list[dict[str, Any]]:
    """Lo que el texto no puede traer (D11): links, telefonos o mails, y montos de dinero."""
    blocks: list[dict[str, Any]] = []
    if _LINK.search(text):
        blocks.append(_finding("temporary_instruction_link", "texto"))
    if looks_like_identifier(text):
        blocks.append(_finding("temporary_instruction_identifier", "texto"))
    if extract_amounts(text):
        # El precio sale del conocimiento; la misma lectura que la guarda de precio.
        blocks.append(_finding("temporary_instruction_amount", "texto"))
    return blocks


def instruction_blocks(draft: InstructionDraft, *, now: datetime) -> list[dict[str, Any]]:
    """Las reglas al proponer: las del texto y la ventana (en el futuro, a lo sumo 7 dias)."""
    blocks = text_blocks(draft.text)
    if draft.valid_until <= now:
        blocks.append(_finding("temporary_instruction_window_over", "hasta"))
    elif draft.valid_until > now + timedelta(days=MAX_DAYS):
        blocks.append(_finding("temporary_instruction_window_too_long", "hasta"))
    return blocks


def _zone(time_zone: str | None) -> tuple[Any, str]:
    try:
        return ZoneInfo(time_zone or "UTC"), time_zone or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        return UTC, "UTC"


def when_text(valid_until: datetime, time_zone: str | None) -> str:
    """«28/11/2026 a las 23:59 (hora de America/Mexico_City)»: lo que el agente y la pagina leen."""
    zone, name = _zone(time_zone)
    local = valid_until.astimezone(zone)
    return f"{local:%d/%m/%Y} a las {local:%H:%M} (hora de {name})"


def render(text: str, valid_until: datetime, *, time_zone: str | None) -> str:
    """El ``system`` del turno: el preambulo fijo del producto y el texto aprobado."""
    return (
        f"{HEADING}\n"
        f"Esto es una instrucción temporal del equipo, vigente hasta el {when_text(valid_until, time_zone)}. "
        "Aplícala en esta conversación además de todo lo anterior. No cambia los precios, los links ni las "
        "condiciones del producto, y no habilita prometer nada que el conocimiento no diga. No la cites ni "
        "digas que te la pasaron.\n\n"
        f"{text}"
    )


@dataclass(frozen=True)
class TemporaryInstructionBlock:
    """La instruccion de un turno, con la forma de un ``system`` extra de Hermes.

    ``key`` es el id de la instruccion: la procedencia guarda id, version, sha256 y
    ``kind``, nunca el texto (D-53.9).
    """

    instruction_id: str
    number: int
    valid_until: datetime
    rendered: str
    kind: str = KIND

    @property
    def key(self) -> str:
        return self.instruction_id

    @property
    def version(self) -> int:
        return self.number

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.rendered.encode("utf-8")).hexdigest()


# Lo que hace que una publicada no se sirva. Lo dicen /ready y la pagina.
NOT_SERVED_CONTENT = "contenido_invalido"
NOT_SERVED_OVER = "vencida"


class LiveTemporaryInstruction:
    """La instruccion publicada de la instancia, leida de la base con cache corta.

    ``fetch`` devuelve ``{"instruction": {...} | None}`` de
    ``get_live_agent_temporary_instruction_v1``. Si falla, queda lo ultimo bueno
    (o nada, si nunca leyo). ``block(now)`` mira el ``hasta`` en cada turno.
    """

    def __init__(self, *, fetch: Callable[[], Awaitable[Mapping[str, Any]]], time_zone: str | None,
                 refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._fetch = fetch
        self._time_zone = time_zone
        self._refresh_seconds = refresh_seconds
        self._block: TemporaryInstructionBlock | None = None
        self._not_served: tuple[str, str] | None = None
        self._applied: tuple[str, str] | None = None
        self._failures = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def time_zone(self) -> str | None:
        return self._time_zone

    def block(self, now: datetime) -> TemporaryInstructionBlock | None:
        """La que va en este turno, o None. Una vencida no va aunque la base no haya contestado."""
        current = self._block
        if current is None or current.valid_until <= now:
            return None
        return current

    def serving_state(self, instruction_id: str, now: datetime) -> str | None:
        """``served``, el motivo por el que no se sirve, o None si no es la que leyo el bridge."""
        current = self._block
        if current is not None and current.instruction_id == instruction_id:
            return "served" if current.valid_until > now else NOT_SERVED_OVER
        if self._not_served is not None and self._not_served[0] == instruction_id:
            return self._not_served[1]
        return None

    def status_line(self, now: datetime) -> str:
        """Para ``/ready``: que se sirve y si la lectura viene andando."""
        refresh = "ok" if self._failures == 0 else f"failing:{self._failures}"
        current = self.block(now)
        if current is not None:
            return f"served:v{current.version},refresh={refresh}"
        if self._not_served is not None:
            return f"not_served:{self._not_served[1]},refresh={refresh}"
        return f"none,refresh={refresh}"

    def _load(self, row: Mapping[str, Any]) -> TemporaryInstructionBlock:
        instruction_id = row["instruction_id"]
        number = row["version"]
        if not isinstance(instruction_id, str) or type(number) is not int or number < 1:
            raise TypeError("instruction")
        text = parse_text(row["texto"])
        until = parse_until(str(row["valid_until"]).replace(" ", "T", 1))
        if text_blocks(text):
            raise InstructionInvalid("temporary_instruction_blocked", {"field": "texto"})
        return TemporaryInstructionBlock(instruction_id=instruction_id, number=number, valid_until=until,
                                         rendered=render(text, until, time_zone=self._time_zone))

    async def refresh(self) -> bool:
        """Lee la base y actualiza. Nunca lanza. False si no se pudo leer."""
        try:
            payload = await self._fetch()
            row = payload["instruction"]
            if row is not None and not isinstance(row, Mapping):
                raise TypeError("instruction")
        except Exception as error:  # noqa: BLE001 - la base caida no tira el turno de nadie
            self._failures += 1
            logger.warning("agent_temporary_instruction_refresh_failed failures=%s error=%s", self._failures,
                           type(error).__name__)
            return False
        self._failures = 0
        key = None if row is None else (str(row.get("instruction_id")), str(row.get("fingerprint")))
        if key == self._applied:
            return True
        if row is None:
            if self._block is not None:
                logger.info("agent_temporary_instruction_cleared id=%s version=%s", self._block.instruction_id,
                            self._block.version)
            self._block, self._not_served, self._applied = None, None, None
            return True
        try:
            loaded = self._load(row)
        except (InstructionInvalid, KeyError, TypeError, ValueError) as error:
            self._block = None
            self._not_served = (str(row.get("instruction_id")), NOT_SERVED_CONTENT)
            self._applied = key
            logger.warning("agent_temporary_instruction_not_served id=%s version=%s reason=%s error=%s",
                           row.get("instruction_id"), row.get("version"), NOT_SERVED_CONTENT, type(error).__name__)
            return True
        self._block, self._not_served, self._applied = loaded, None, key
        logger.info("agent_temporary_instruction_served id=%s version=%s until=%s sha256=%s", loaded.instruction_id,
                    loaded.version, loaded.valid_until.isoformat(), loaded.sha256)
        return True

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="agent-temporary-instruction-refresh")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            await self.refresh()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def utc_now() -> datetime:
    return datetime.now(UTC)
