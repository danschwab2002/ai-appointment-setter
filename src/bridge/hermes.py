"""Hermes API client for non-sending shadow evaluations."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx

from bridge.agent_provenance import AgentTurn, build_turn, external_conversation_id
from bridge.campaign_inbound_entry import CAMPAIGN_ANSWER_KEY
from bridge.price_guard import (
    OUTCOME_HANDOFF,
    OUTCOME_PASSED,
    OUTCOME_RETRIED_OK,
    PRICE_NOT_IN_CATALOG,
    Amount,
    PriceGuard,
    correction_detail,
    provenance_entry,
)


_CAPTURED_FIELDS = {
    "person_name",
    "location",
    "role",
    "company_name",
    "company_size",
    "business_model",
    "company_operational",
    "can_invest_in_education",
}

_TEXT_CAPTURED_FIELDS = {
    "person_name",
    "location",
    "role",
    "company_name",
    "business_model",
}

_BOOLEAN_CAPTURED_FIELDS = {
    "company_operational",
    "can_invest_in_education",
}

_DECISION_STATUSES = {
    "ask_question": "in_progress",
    "send_payment_link": "in_progress",
    "qualified": "qualified",
    "disqualified": "disqualified",
    "handoff": "needs_human",
}

_MAX_PROPOSAL_ATTEMPTS = 2

logger = logging.getLogger(__name__)

# E-175: el motivo de la derivacion de un turno del agente que fallo, y los
# resultados `failed` que derivan. `chatwoot_history_not_configured` no esta:
# no es una falla del agente sino de la instalacion, y derivar no la arregla.
AGENT_TURN_FAILED_REASON_CODE = "agent_turn_failed"
# E-564 (price-guard-v1 §4): la guarda de precio bloqueo los dos intentos. Solo
# aparece con AGENT_PRICE_GUARD_MODE=bloquea; deriva con su propio motivo.
_AGENT_TURN_FAILURES_THAT_HAND_OFF = frozenset(
    {"invalid_agent_output", "hermes_unavailable", PRICE_NOT_IN_CATALOG}
)


def agent_turn_failure_to_hand_off(
    shadow_processor: object | None, *, delivery_id: str
) -> str | None:
    """El motivo de un turno fallido que tiene que derivar, o None.

    `get_failure_reason` es opcional en el procesador: uno que no lo tiene
    (un doble de test viejo) se porta como antes de E-175.
    """
    reader = getattr(shadow_processor, "get_failure_reason", None)
    if reader is None:
        return None
    reason = reader(delivery_id=delivery_id)
    if reason in _AGENT_TURN_FAILURES_THAT_HAND_OFF:
        return reason
    return None


def _idempotency_key(digest: str, attempt: int) -> str:
    """Return one Idempotency-Key per attempt.

    The Hermes api_server replays the same content for an Idempotency-Key it
    has already seen: measured on 2026-09-25, two requests carrying the same
    key returned byte-identical content and token counts, while requests with
    a different key (or none) returned fresh content. Retrying under the first
    attempt's key would therefore replay the malformed proposal verbatim.

    The first attempt keeps the bare digest so that reprocessing the same
    delivery stays idempotent, which is what the header was added for.
    """
    if attempt <= 1:
        return digest
    return f"{digest}-retry-{attempt}"


_PARSED_FROM_PROSE_THEN_JSON = "prose_then_json"


def _parse_agent_proposal(content: object) -> dict[str, object] | None:
    proposal, _ = _parse_agent_proposal_with_origin(content)
    return proposal


def _parse_agent_proposal_with_origin(
    content: object,
) -> tuple[dict[str, object] | None, str | None]:
    """La propuesta del texto del modelo, y de donde salio.

    El origen es None cuando el texto es la propuesta (entera o en un fence),
    y `prose_then_json` cuando se rescato del final de un texto en prosa.
    """
    proposal = _parse_whole_agent_proposal(content)
    if proposal is not None:
        return proposal, None
    rescued = _rescue_trailing_agent_proposal(content)
    if rescued is not None:
        return rescued, _PARSED_FROM_PROSE_THEN_JSON
    return None, None


def _rescue_trailing_agent_proposal(content: object) -> dict[str, object] | None:
    """El objeto JSON con el que termina un texto en prosa, o None.

    GLM 5.3 Flash a veces escribe primero la respuesta en prosa y despues la
    propuesta (fixtures hermes_glm_prose_then_json_*): en Maribel, el 28/10,
    dos turnos seguidos de la conversacion 9 terminaron en invalid_agent_output
    con una propuesta valida al final del texto, y la persona se quedo sin
    respuesta. La regla es estricta a proposito, para no inventar nada:

    - el texto termina en el '}' que cierra el objeto (solo espacios despues);
    - el objeto es uno solo y completo: la prosa de antes no tiene '{' ni '}',
      asi que no hay dos candidatos ni un objeto partido;
    - la prosa nunca se manda: de aca sale solo el objeto, y el que llama lo
      pasa por el mismo validador que a una propuesta entera.
    """
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped.endswith("}"):
        return None
    start = stripped.find("{")
    if start <= 0:
        # Sin prosa delante, el texto ya se probo entero y no era una propuesta.
        return None
    if "}" in stripped[:start]:
        return None
    try:
        proposal, end = json.JSONDecoder(strict=False).raw_decode(stripped, start)
    except json.JSONDecodeError:
        return None
    if end != len(stripped) or not isinstance(proposal, dict):
        return None
    return proposal


def _parse_whole_agent_proposal(content: object) -> dict[str, object] | None:
    if not isinstance(content, str):
        return None

    stripped = content.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        opening_end = stripped.find("\n")
        closing_start = stripped.rfind("```")
        opening = (
            stripped[:opening_end].strip().lower()
            if opening_end >= 0
            else ""
        )
        if (
            opening in {"```", "```json"}
            and closing_start > opening_end
            and closing_start == len(stripped) - 3
        ):
            candidates.append(stripped[opening_end + 1 : closing_start].strip())

    for candidate in candidates:
        try:
            # strict=False deja pasar saltos de linea literales dentro de un
            # string: GLM 5.3 Flash a veces separa los parrafos del reply asi
            # (fixture hermes_glm_proposal_raw_newlines_20261012.json), y el
            # resto de la propuesta es valido.
            proposal = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(proposal, dict):
            return proposal
    return None


MAX_REPLY_CHARS = 1000

_PROPOSAL_KEYS = frozenset({
    "decision",
    "qualification_status",
    "reason_code",
    "reply",
    "captured_fields",
    "missing_fields",
})


def payment_link_product(
    proposal: dict[str, object], product_keys: tuple[str, ...]
) -> str | None:
    """De que producto es el link que pide el agente (2b).

    El que dijo el agente en `product`. Si no lo dijo y la instancia declara
    un solo producto, ese: asi un SOUL que todavia no conoce la clave no deja
    a nadie sin link. Sin productos declarados, None.
    """
    product = proposal.get("product")
    if isinstance(product, str):
        return product
    if len(product_keys) == 1:
        return product_keys[0]
    return None


def proposal_rejection_reason(
    proposal: object,
    product_keys: frozenset[str] = frozenset(),
) -> str | None:
    """La regla que rompe una propuesta, o None si es valida (E-486).

    Es la misma validacion de siempre, pero dice QUE fallo: alimenta el log
    `agent_output_invalid` y el mensaje con el que se corrige el reintento.
    Las reglas son nombres fijos, nunca texto de la persona ni del modelo.
    """
    if not isinstance(proposal, dict):
        return "not_json"
    # Las seis claves de siempre. Con productos declarados en el manifiesto
    # (2b), una septima opcional, `product`: null o la clave de un producto
    # declarado. Sin productos, `product` invalida la propuesta como cualquier
    # otra clave de mas: Johanna no cambia.
    keys = set(proposal)
    if keys == _PROPOSAL_KEYS:
        product = None
    elif product_keys and keys == _PROPOSAL_KEYS | {"product"}:
        product = proposal["product"]
        if product is not None and (
            not isinstance(product, str) or product not in product_keys
        ):
            return "unknown_product"
    else:
        return "extra_key"

    decision = proposal["decision"]
    if not isinstance(decision, str) or decision not in _DECISION_STATUSES:
        return "decision"
    # Con dos productos o mas, el link sin producto no se puede armar.
    if decision == "send_payment_link" and product is None and len(product_keys) > 1:
        return "link_without_product"
    if proposal["qualification_status"] != _DECISION_STATUSES.get(decision):
        return "qualification_status"

    reason_code = proposal["reason_code"]
    if not isinstance(reason_code, str) or re.fullmatch(
        r"[a-z0-9_]{1,64}", reason_code
    ) is None:
        return "reason_code"

    reply = proposal["reply"]
    if not isinstance(reply, str) or not reply:
        return "reply_empty"
    if len(reply) > MAX_REPLY_CHARS:
        return "reply_too_long"
    if reply.count("?") > 1:
        return "too_many_questions"
    if decision == "send_payment_link" and any(
        marker in reply.lower() for marker in ("http://", "https://", "www.")
    ):
        return "url_in_reply"

    captured = proposal["captured_fields"]
    if not isinstance(captured, dict) or set(captured) != _CAPTURED_FIELDS:
        return "schema"
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 500
        )
        for field, value in captured.items()
        if field in _TEXT_CAPTURED_FIELDS
    ):
        return "schema"
    company_size = captured["company_size"]
    if company_size is not None and (
        not isinstance(company_size, int)
        or isinstance(company_size, bool)
        or company_size <= 0
    ):
        return "schema"
    if any(
        value is not None and not isinstance(value, bool)
        for field, value in captured.items()
        if field in _BOOLEAN_CAPTURED_FIELDS
    ):
        return "schema"

    missing = proposal["missing_fields"]
    if not isinstance(missing, list):
        return "schema"
    if not all(isinstance(field, str) for field in missing):
        return "schema"
    if len(missing) != len(set(missing)):
        return "schema"
    if not all(
        isinstance(field, str)
        and field in _CAPTURED_FIELDS
        and captured[field] is None
        for field in missing
    ):
        return "schema"
    return None


def _is_valid_proposal(
    proposal: dict[str, object],
    product_keys: frozenset[str] = frozenset(),
) -> bool:
    # Las seis claves de siempre. Con productos declarados en el manifiesto
    # (2b), una septima opcional, `product`: null o la clave de un producto
    # declarado. Sin productos, `product` invalida la propuesta como cualquier
    # otra clave de mas: Johanna no cambia. (El procesador la descarta antes
    # de validar: `normalize_proposal`, E-486.)
    return proposal_rejection_reason(proposal, product_keys) is None


def normalize_proposal(
    proposal: dict[str, object], product_keys: frozenset[str]
) -> dict[str, object]:
    """Saca lo inofensivo antes de validar (E-486).

    Sin productos declarados en la instancia, una clave `product` no tiene a
    que apuntar: el link sale igual sin producto (`payment_link_product`
    devuelve None). Maribel #120 y ATT1 #726: el modelo la sumaba al hablar de
    "el producto" y el turno entero terminaba derivado. Con productos
    declarados no se toca nada: un producto que no existe sigue invalidando.
    """
    if not product_keys and "product" in proposal:
        cleaned = dict(proposal)
        cleaned.pop("product")
        return cleaned
    return proposal


# Lo que se le dice al modelo en el reintento, por regla. Texto fijo del
# producto: nunca lleva lo que escribio la persona ni lo que contesto el modelo.
_RETRY_CORRECTIONS = {
    "not_json": "no era un objeto JSON. Devolve solo el objeto JSON, sin texto antes ni despues.",
    "extra_key": "tenia claves de mas. Usa solo las claves del formato.",
    "unknown_product": "`product` no es uno de los productos de la entrada.",
    "decision": "`decision` no es uno de los valores permitidos.",
    "link_without_product": "falta `product` para pedir el enlace.",
    "qualification_status": "`qualification_status` no corresponde a `decision`.",
    "reason_code": "`reason_code` tiene que ser minusculas, numeros y guiones bajos.",
    "reply_empty": "`reply` esta vacio.",
    "too_many_questions": "`reply` tiene mas de una pregunta. Deja una sola.",
    "url_in_reply": "`reply` trae una direccion web; el enlace lo manda el sistema.",
    "schema": "`captured_fields` o `missing_fields` no tienen la forma pedida.",
}


def retry_correction(
    rule: str, proposal: object = None, *, price_detail: str | None = None
) -> str:
    """El mensaje corto que corrige el segundo intento (E-486).

    ``price_detail`` (E-564, price-guard-v1 §4): lo que dijo la guarda de
    precio en el mismo intento. Si fue la unica regla, es el detalle; si el
    intento ademas rompio otra regla, la correccion las dice juntas. Sin el,
    el mensaje es el de siempre.
    """
    if rule == PRICE_NOT_IN_CATALOG and price_detail:
        return (
            f"Tu respuesta anterior no se pudo usar: {price_detail} "
            "Devolve solo el objeto JSON."
        )
    if rule == "reply_too_long" and isinstance(proposal, dict):
        reply = proposal.get("reply")
        size = len(reply) if isinstance(reply, str) else 0
        detail = (
            f"`reply` tiene {size} caracteres y el maximo es {MAX_REPLY_CHARS}. "
            "Acortalo."
        )
    elif rule == "reply_too_long":
        detail = f"`reply` supera los {MAX_REPLY_CHARS} caracteres. Acortalo."
    else:
        detail = _RETRY_CORRECTIONS.get(rule, "no cumplia el formato.")
    if price_detail:
        detail = f"{detail} Ademas, {price_detail}"
    return (
        f"Tu respuesta anterior no se pudo usar: {detail} "
        "Devolve solo el objeto JSON."
    )




def _guard_kwargs(record: dict[str, object] | None) -> dict[str, object]:
    """La clave de la guarda para `_record_turn`, solo si hay algo que anotar."""
    return {"price_guard": record} if record is not None else {}


class TurnProvenanceRecorder(Protocol):
    """Quien sabe grabar la procedencia de un turno.

    Es un protocolo y no una dependencia concreta para que este modulo no
    aprenda de Supabase: el procesador sabe QUE paso en el turno, no donde
    se anota. El cableado vive en `bridge.app`.
    """

    async def record_agent_turn(self, *, turn: AgentTurn) -> None:
        ...  # pragma: no cover - protocolo


class ExtraSystemBlock(Protocol):
    """Un `system` de un turno, despues del conocimiento base (E-311).

    Hoy, un segmento vigente por etiqueta (`bridge.segment_knowledge.LoadedSegment`).
    El procesador solo lee esto: el texto que manda y lo que anota en la
    procedencia del turno.
    """

    @property
    def key(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def rendered(self) -> str: ...


class HermesShadowProcessor:
    """Request and persist an agent proposal without executing it."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        shadow_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        provenance_recorder: TurnProvenanceRecorder | None = None,
        system_prompt: str | None = None,
        product_keys: tuple[str, ...] = (),
        campaign_answers: bool = False,
        price_guard: PriceGuard | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # E-564 (price-guard-v1): con AGENT_PRICE_GUARD_MODE=bloquea, la guarda
        # de precio de la instancia. None (el flag en off), el procesador no
        # extrae nada y todo es byte a byte como antes.
        self._price_guard = price_guard
        # E-317 (campaign-inbound-entry-v1): con la entrada a las campanas prendida, la
        # salida puede traer `campana_respuesta` ("si" | "no"). Se saca de la
        # propuesta antes de validarla y se guarda aparte: la propuesta queda con
        # la forma de siempre para todo el que la lee. Apagado, una clave de mas
        # invalida la propuesta, como hoy.
        self._campaign_answers = campaign_answers
        # Las claves de [[productos]] del manifiesto: las unicas que acepta
        # `product` en la propuesta. Vacio, la salida es la de siempre.
        self._product_keys = frozenset(product_keys)
        self._api_key = api_key
        self._model_name = model_name
        # El conocimiento comercial de la instancia (contracts/commercial-knowledge-v1).
        # Hermes apila los mensajes `system` sobre el prompt del profile, asi que el
        # SOUL comun no cambia y cada instancia pone lo suyo.
        self._system_prompt = system_prompt
        self._shadow_dir = shadow_dir
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._provenance_recorder = provenance_recorder

    def bind_provenance_recorder(
        self, recorder: TurnProvenanceRecorder | None
    ) -> None:
        """Enchufar el registrador despues de construir el procesador.

        El procesador se arma en `build_app`, donde todavia no existe el cliente
        de Supabase; el registrador se enchufa en `create_app`, que ya lo tiene.
        Es explicito y no un atributo privado tocado de afuera para que un doble
        de test pueda no tener el metodo y el cableado lo detecte con hasattr.
        """
        self._provenance_recorder = recorder

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        self._persist_result(
            digest=digest,
            result={
                "status": "failed",
                "delivery_id_hash": digest,
                "reason": reason,
            },
        )

    def has_result(self, *, delivery_id: str) -> bool:
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        return self._is_terminal_result(
            self._shadow_dir / f"{digest}.json",
            digest=digest,
        )

    def get_failure_reason(self, *, delivery_id: str) -> str | None:
        """El motivo del resultado `failed` de un turno, o None si no fallo.

        Lo lee el bridge cuando un turno termina sin propuesta, para derivar
        a una persona en vez de quedarse callado (agent_turn_failed).
        """
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        try:
            result = json.loads(
                (self._shadow_dir / f"{digest}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(result, dict)
            or result.get("delivery_id_hash") != digest
            or result.get("status") != "failed"
        ):
            return None
        reason = result.get("reason")
        return reason if isinstance(reason, str) else None

    def get_price_guard_link_request(
        self, *, delivery_id: str
    ) -> dict[str, str | None] | None:
        """Si el turno derivo por la guarda de precio y el agente pedia el link.

        Devuelve ``{"product": <clave o None>}``: el link no salio (solo sale
        junto con una respuesta que se entrega) y la nota de la derivacion se
        lo dice al equipo (price-guard-v1 §4). None en cualquier otro caso.
        """
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        try:
            result = json.loads(
                (self._shadow_dir / f"{digest}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(result, dict)
            or result.get("delivery_id_hash") != digest
            or result.get("status") != "failed"
            or result.get("reason") != PRICE_NOT_IN_CATALOG
            or result.get("payment_link_requested") is not True
        ):
            return None
        product = result.get("payment_link_product")
        return {"product": product if isinstance(product, str) else None}

    @property
    def accepts_campaign_answers(self) -> bool:
        return self._campaign_answers

    def get_campaign_answer(self, *, delivery_id: str) -> str | None:
        """Lo que contesto la persona a la pregunta de entrada (E-317), o None."""
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        try:
            result = json.loads(
                (self._shadow_dir / f"{digest}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(result, dict)
            or result.get("delivery_id_hash") != digest
            or result.get("status") != "completed"
        ):
            return None
        answer = result.get("campaign_answer")
        return answer if answer in ("si", "no") else None

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None:
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        try:
            result = json.loads(
                (self._shadow_dir / f"{digest}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(result, dict)
            or result.get("delivery_id_hash") != digest
            or result.get("status") != "completed"
        ):
            return None
        proposal = result.get("proposal")
        if not isinstance(proposal, dict) or not _is_valid_proposal(
            proposal, self._product_keys
        ):
            return None
        return proposal

    async def run(
        self,
        *,
        delivery_id: str,
        context: dict[str, object],
        extra_system: Sequence[ExtraSystemBlock] = (),
    ) -> None:
        """Pide la propuesta del turno.

        ``extra_system``: los `system` de este turno que van despues del
        conocimiento base, en su orden (los segmentos, E-311). Vacio, el pedido
        es byte a byte el de siempre. El digest y la clave de idempotencia
        salen del delivery y no cambian.
        """
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        self._shadow_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._shadow_dir.chmod(0o700)
        processing_lock_path = self._shadow_dir / f".{digest}.processing.lock"
        processing_lock_fd = os.open(
            processing_lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.fchmod(processing_lock_fd, 0o600)
        try:
            await self._acquire_processing_lock(processing_lock_fd)
            if self.has_result(delivery_id=delivery_id):
                return
            await self._request_and_persist(
                digest=digest,
                context=context,
                delivery_id=delivery_id,
                extra_system=tuple(extra_system),
            )
        finally:
            fcntl.flock(processing_lock_fd, fcntl.LOCK_UN)
            os.close(processing_lock_fd)

    @staticmethod
    async def _acquire_processing_lock(lock_fd: int) -> None:
        while True:
            try:
                fcntl.flock(
                    lock_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                return
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def _record_turn(
        self,
        *,
        delivery_id: str,
        context: dict[str, object],
        attempt: int,
        outcome: str,
        model_answered: str | None,
        extra_system: tuple[ExtraSystemBlock, ...] = (),
        price_guard: dict[str, object] | None = None,
    ) -> None:
        """Deja el rastro del turno. Nunca rompe el turno."""
        if self._provenance_recorder is None:
            return
        try:
            await self._provenance_recorder.record_agent_turn(
                turn=build_turn(
                    context=context,
                    delivery_id=delivery_id,
                    attempt=attempt,
                    outcome=outcome,
                    model_requested=self._model_name,
                    model_answered=model_answered,
                    segments=tuple(
                        {
                            "key": block.key,
                            "version": block.version,
                            "sha256": block.sha256,
                            # Cuantas respuestas del agente hubo antes del turno
                            # (E-311): el primer turno con el segmento y > 0 es
                            # un segmento que llego tarde.
                            **(
                                {"agent_replies_before": replies}
                                if isinstance(
                                    replies := getattr(block, "agent_replies_before", None), int
                                )
                                else {}
                            ),
                            # E-317: el aviso de campana va con su tipo, para no
                            # contarlo como el bloque de un segmento.
                            **(
                                {"kind": kind}
                                if isinstance(kind := getattr(block, "kind", None), str)
                                else {}
                            ),
                        }
                        for block in extra_system
                    ),
                    **({"price_guard": price_guard} if price_guard is not None else {}),
                )
            )
        except Exception:  # noqa: BLE001 - la propuesta manda
            pass

    async def _request_and_persist(
        self,
        *,
        digest: str,
        context: dict[str, object],
        delivery_id: str,
        extra_system: tuple[ExtraSystemBlock, ...] = (),
    ) -> None:
        # Lo ultimo que Hermes dijo que contesto. Se arrastra fuera del
        # bucle porque el punto terminal de salida invalida esta despues.
        model_answered: str | None = None
        # E-486: la regla que rompio el intento anterior. El reintento la
        # lleva como un mensaje corto, en vez de repetir el mismo pedido.
        correction: str | None = None
        # E-564 (price-guard-v1 §3): los montos permitidos de este turno, de los
        # mismos textos que recibe el modelo. Solo con la guarda prendida.
        guard = self._price_guard
        allowed: frozenset[Amount] = (
            guard.allowed_for_turn(
                texts=(self._system_prompt,),
                context=context,
                blocks=extra_system,
            )
            if guard is not None
            else frozenset()
        )
        guard_attempts: list[dict[str, object]] = []
        last_rule: str | None = None
        last_proposal: object = None

        def guard_record(outcome: str) -> dict[str, object] | None:
            if guard is None or not guard_attempts:
                return None
            return provenance_entry(guard_attempts, outcome)

        for attempt in range(1, _MAX_PROPOSAL_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout_seconds,
                ) as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Idempotency-Key": _idempotency_key(digest, attempt),
                        },
                        json={
                            "model": self._model_name,
                            "stream": False,
                            "messages": [
                                *(
                                    [{"role": "system", "content": self._system_prompt}]
                                    if self._system_prompt
                                    else []
                                ),
                                *(
                                    {"role": "system", "content": block.rendered}
                                    for block in extra_system
                                ),
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        context, ensure_ascii=False
                                    ),
                                },
                                *(
                                    [{"role": "user", "content": correction}]
                                    if correction
                                    else []
                                ),
                            ],
                        },
                    )
                    response.raise_for_status()
            except httpx.HTTPError:
                self._persist_result(
                    digest=digest,
                    result=self._attempted(
                        {
                            "status": "failed",
                            "delivery_id_hash": digest,
                            "reason": "hermes_unavailable",
                        },
                        attempt=attempt,
                    ),
                )
                await self._record_turn(
                    delivery_id=delivery_id,
                    context=context,
                    attempt=attempt,
                    outcome="failed",
                    model_answered=None,
                    extra_system=extra_system,
                    **_guard_kwargs(guard_record(OUTCOME_HANDOFF)),
                )
                return

            try:
                body = response.json()
                # El cuerpo se descartaba entero salvo el texto. `model` es
                # el unico rastro de QUE contesto, y venia gratis.
                if isinstance(body, dict) and isinstance(body.get("model"), str):
                    model_answered = body["model"][:128]
                proposal_text = body["choices"][0]["message"]["content"]
            except (
                KeyError,
                IndexError,
                TypeError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                proposal = None
                parsed_from = None
            else:
                proposal, parsed_from = _parse_agent_proposal_with_origin(
                    proposal_text
                )

            campaign_answer: str | None = None
            if (
                self._campaign_answers
                and isinstance(proposal, dict)
                and CAMPAIGN_ANSWER_KEY in proposal
            ):
                proposal = dict(proposal)
                raw_answer = proposal.pop(CAMPAIGN_ANSWER_KEY)
                # Otro valor (null, "tal vez") no invalida la respuesta: no se
                # registra nada, como sin respuesta clara.
                campaign_answer = raw_answer if raw_answer in ("si", "no") else None

            if isinstance(proposal, dict):
                proposal = normalize_proposal(proposal, self._product_keys)
            rule = proposal_rejection_reason(proposal, self._product_keys)
            # E-564 (price-guard-v1 §4): la guarda mira el `reply` del intento.
            # No mira el de una derivacion: al derivar el bridge no manda el
            # reply (F-033) y cambiarle el motivo perderia, por ejemplo, el
            # aviso de seguridad (D1 del PR).
            blocked: tuple[Amount, ...] = ()
            if guard is not None:
                if isinstance(proposal, dict) and proposal.get("decision") != "handoff":
                    blocked = guard.blocked(proposal.get("reply"), allowed)
                guard_attempts.append(
                    {
                        "attempt": attempt,
                        "blocked": [amount.as_json() for amount in blocked],
                    }
                )
                if blocked:
                    blocked_conversation = external_conversation_id(context)
                    logger.info(
                        "agent_price_guard_blocked attempt=%d conversation=%s amounts=%d",
                        attempt,
                        blocked_conversation
                        if blocked_conversation is not None
                        else "-",
                        len(blocked),
                    )
                    if rule is None:
                        rule = PRICE_NOT_IN_CATALOG
            if rule is None and isinstance(proposal, dict):
                completed: dict[str, object] = {
                    "status": "completed",
                    "delivery_id_hash": digest,
                    "proposal": proposal,
                }
                if parsed_from is not None:
                    # Senal positiva, como `attempts`: solo aparece en los
                    # turnos rescatados de la prosa, para poder contarlos.
                    completed["parsed_from"] = parsed_from
                if campaign_answer is not None:
                    completed["campaign_answer"] = campaign_answer
                self._persist_result(
                    digest=digest,
                    result=self._attempted(completed, attempt=attempt),
                )
                await self._record_turn(
                    delivery_id=delivery_id,
                    context=context,
                    attempt=attempt,
                    outcome="completed",
                    model_answered=model_answered,
                    extra_system=extra_system,
                    **_guard_kwargs(
                        guard_record(
                            OUTCOME_RETRIED_OK
                            if any(entry["blocked"] for entry in guard_attempts)
                            else OUTCOME_PASSED
                        )
                    ),
                )
                return

            # Sin contenido: la regla, el intento y el numero de conversacion.
            conversation = external_conversation_id(context)
            logger.info(
                "agent_output_invalid attempt=%d rule=%s conversation=%s",
                attempt,
                rule,
                conversation if conversation is not None else "-",
            )
            correction = retry_correction(
                rule or "not_json",
                proposal,
                **(
                    {"price_detail": correction_detail(blocked, allowed)}
                    if blocked
                    else {}
                ),
            )
            last_rule = rule
            last_proposal = proposal

        failure: dict[str, object] = {
            "status": "failed",
            "delivery_id_hash": digest,
            "reason": "invalid_agent_output",
        }
        if last_rule == PRICE_NOT_IN_CATALOG and isinstance(last_proposal, dict):
            # Manda la regla del ultimo intento (price-guard-v1 §4.4): la guarda
            # bloqueo una propuesta valida en su forma. Si pedia el link, el
            # link no sale y la nota de la derivacion lo dice.
            failure["reason"] = PRICE_NOT_IN_CATALOG
            if last_proposal.get("decision") == "send_payment_link":
                failure["payment_link_requested"] = True
                link_product = payment_link_product(
                    last_proposal, tuple(sorted(self._product_keys))
                )
                if link_product is not None:
                    failure["payment_link_product"] = link_product
        self._persist_result(
            digest=digest,
            result=self._attempted(failure, attempt=_MAX_PROPOSAL_ATTEMPTS),
        )
        await self._record_turn(
            delivery_id=delivery_id,
            context=context,
            attempt=_MAX_PROPOSAL_ATTEMPTS,
            outcome="failed",
            model_answered=model_answered,
            extra_system=extra_system,
            **_guard_kwargs(guard_record(OUTCOME_HANDOFF)),
        )

    @staticmethod
    def _attempted(
        result: dict[str, object], *, attempt: int
    ) -> dict[str, object]:
        """Record how many attempts it took, but only when it took more than one.

        A single-attempt result keeps the historical shape, so the field is a
        positive signal: every shadow carrying `attempts` is a turn where the
        agent's first proposal was unusable, which is the rate this change
        exists to watch.
        """
        if attempt > 1:
            result["attempts"] = attempt
        return result

    def _persist_result(
        self, *, digest: str, result: dict[str, object]
    ) -> None:
        self._shadow_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._shadow_dir.chmod(0o700)
        result_path = self._shadow_dir / f"{digest}.json"
        lock_path = self._shadow_dir / f".{digest}.lock"
        serialized = (
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd) as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            if self._is_terminal_result(result_path, digest=digest):
                return

            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=self._shadow_dir,
            )
            try:
                os.fchmod(temporary_fd, 0o600)
                with os.fdopen(
                    temporary_fd,
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, result_path)
                directory_fd = os.open(
                    self._shadow_dir,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _is_terminal_result(self, path: Path, *, digest: str) -> bool:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(result, dict):
            return False
        if result.get("delivery_id_hash") != digest:
            return False
        if result.get("status") == "failed":
            return isinstance(result.get("reason"), str)
        proposal = result.get("proposal")
        return (
            result.get("status") == "completed"
            and isinstance(proposal, dict)
            and _is_valid_proposal(proposal, self._product_keys)
        )
