"""Un turno de ensayo del agente comercial, sin Hermes ni Chatwoot: nadie recibe nada.

Lo usan dos piezas de la serie Z1, cada una detras de su flag:

* el arnes F-055 sobre un borrador del conocimiento (``knowledge_harness``,
  ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED``), y
* «probarla conversando» una campana validada (``campaign_conversation``, 4.9,
  ``AUTOMATION_EXPERT_CAMPAIGN_CONVERSATION_ENABLED``).

Es el mismo camino que el arnes de las instancias (``conocimiento/prueba/probar.py`` del repo de
cada una, E-194) y que el juez (``segment_check.openrouter_judge``): el SOUL comun del producto como
primer ``system``, lo que la instancia le pone encima (el conocimiento renderizado y, en una campana,
su bloque) y el contexto JSON con la forma del arnes como mensaje del usuario, directo a OpenRouter
con la clave del bridge. La diferencia con el arnes de las instancias (D-REH.1): la propuesta se lee
con las MISMAS funciones del bridge (``hermes._parse_agent_proposal_with_origin``,
``normalize_proposal`` y ``proposal_rejection_reason``) y, como el bridge, con hasta dos intentos y
la correccion del segundo (``retry_correction``): lo que el ensayo da por bueno es lo que el bridge
le habria mandado a la persona.

Lo que no hace: la guarda de precio, la instruccion temporal, la entrada a campanas ni nada que
dependa de una conversacion real de Chatwoot. Nunca escribe en la base ni manda mensajes.

El modelo (D-REH.2): ``AGENT_REHEARSAL_MODEL`` del entorno o, si no esta, el ``modelo`` de
``[profiles.agente-comercial]`` del ``hermes.toml`` de la carpeta de la instancia (el mismo que lee
el arnes de las instancias). Sin ninguno de los dos, el flag que lo pide no arranca.

Los logs llevan el codigo y el tipo de error; nunca la clave, el texto del conocimiento ni lo que
contesto el modelo.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .hermes import (
    _MAX_PROPOSAL_ATTEMPTS,
    _parse_agent_proposal_with_origin,
    normalize_proposal,
    proposal_rejection_reason,
    retry_correction,
)

__all__ = [
    "MODEL_ENV",
    "Answer",
    "Ask",
    "RehearsalError",
    "TurnResult",
    "agent_model",
    "build_context",
    "build_messages",
    "openrouter_ask",
    "rehearse_turn",
]

MODEL_ENV = "AGENT_REHEARSAL_MODEL"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_ACTORS = frozenset({"prospect", "assistant"})


class RehearsalError(RuntimeError):
    """El modelo no contesto (red, HTTP o una respuesta sin texto). El mensaje no lleva la clave."""


@dataclass(frozen=True)
class Answer:
    model: str | None
    content: str


Ask = Callable[[list[dict[str, str]]], Answer]


def openrouter_ask(*, model: str, api_key: str, transport: httpx.BaseTransport | None = None) -> Ask:
    """El pedido a OpenRouter, sincrono (corre en un hilo). Cada llamada abre su cliente."""

    def ask(messages: list[dict[str, str]]) -> Answer:
        try:
            with httpx.Client(transport=transport, timeout=_TIMEOUT) as client:
                response = client.post(OPENROUTER_URL, json={"model": model, "stream": False, "messages": messages},
                                       headers={"Authorization": f"Bearer {api_key}"})
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RehearsalError(f"openrouter: {type(error).__name__}") from None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RehearsalError("openrouter: respuesta sin texto") from None
        if not isinstance(content, str):
            raise RehearsalError("openrouter: respuesta sin texto")
        answered = body.get("model") if isinstance(body, Mapping) else None
        return Answer(model=answered[:128] if isinstance(answered, str) else None, content=content)

    return ask


def agent_model(instance_dir: Path | None, environ: Mapping[str, str] | None = None) -> str | None:
    """El modelo del agente comercial: el del entorno o el de ``hermes.toml`` de la instancia."""
    env = os.environ if environ is None else environ
    explicit = env.get(MODEL_ENV, "").strip()
    if explicit:
        return explicit
    if instance_dir is None:
        return None
    path = Path(instance_dir) / "hermes.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    profile = data.get("profiles", {}).get("agente-comercial", {}) if isinstance(data.get("profiles"), Mapping) \
        else {}
    model = profile.get("modelo") if isinstance(profile, Mapping) else None
    return model.strip() if isinstance(model, str) and model.strip() else None


def build_context(turns: Sequence[tuple[str, str]], *, extra: Mapping[str, Any] | None = None,
                  coupon_applied: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """El contexto con la forma del arnes F-055 (``probar.context``): un mensaje por minuto hasta ahora.

    ``turns`` son pares ``(actor, texto)`` con actor ``prospect`` o ``assistant``."""
    moment = now or datetime.now(UTC)
    first = moment - timedelta(minutes=len(turns))
    messages = []
    for index, (actor, text) in enumerate(turns):
        if actor not in _ACTORS:
            raise ValueError("actor")
        sent = (first + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        messages.append({"actor": actor, "text": text, "sent_at": sent})
    context: dict[str, Any] = {
        "conversation_ref": "ensayo",
        "human_handoff_confirmed": False,
        "known_fields": {},
        "messages": messages,
        "payment_link_action": {
            "enabled": True,
            "decision": "send_payment_link",
            "bridge_injects_exact_url": True,
            "agent_must_not_include_url": True,
            "coupon_applied": bool(coupon_applied),
        },
    }
    context.update(dict(extra or {}))
    return context


def build_messages(*, soul: str, systems: Sequence[str], context: Mapping[str, Any],
                   correction: str | None = None) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": soul},
        *({"role": "system", "content": text} for text in systems if text),
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        *([{"role": "user", "content": correction}] if correction else []),
    ]


@dataclass(frozen=True)
class TurnResult:
    """Lo que habria hecho el bridge con el turno: la propuesta valida, o la regla que rompio."""

    valid: bool
    rejection: str | None
    decision: str | None
    reason_code: str | None
    reply: str | None
    attempts: int
    model: str | None
    seconds: float

    def row(self) -> dict[str, Any]:
        return {"valid": self.valid, "rechazo": self.rejection, "decision": self.decision,
                "reason_code": self.reason_code, "reply": self.reply, "intentos": self.attempts,
                "modelo": self.model}


def rehearse_turn(ask: Ask, *, soul: str, systems: Sequence[str], context: Mapping[str, Any],
                  product_keys: frozenset[str] = frozenset()) -> TurnResult:
    """Un turno como lo corre el bridge: hasta dos intentos, el segundo con la correccion de la regla.

    ``RehearsalError`` (el modelo no contesto) sale para arriba: no es una propuesta invalida."""
    started = time.monotonic()
    correction: str | None = None
    rule: str | None = None
    proposal: object = None
    model: str | None = None
    for attempt in range(1, _MAX_PROPOSAL_ATTEMPTS + 1):
        answer = ask(build_messages(soul=soul, systems=systems, context=context, correction=correction))
        model = answer.model or model
        parsed, _origin = _parse_agent_proposal_with_origin(answer.content)
        proposal = normalize_proposal(parsed, product_keys) if isinstance(parsed, dict) else parsed
        rule = proposal_rejection_reason(proposal, product_keys)
        if rule is None:
            assert isinstance(proposal, dict)
            reply = proposal.get("reply")
            return TurnResult(valid=True, rejection=None, decision=_text(proposal.get("decision")),
                              reason_code=_text(proposal.get("reason_code")),
                              reply=reply if isinstance(reply, str) else None, attempts=attempt, model=model,
                              seconds=time.monotonic() - started)
        correction = retry_correction(rule, proposal)
    shown = proposal if isinstance(proposal, dict) else {}
    reply = shown.get("reply")
    return TurnResult(valid=False, rejection=rule, decision=_text(shown.get("decision")),
                      reason_code=_text(shown.get("reason_code")), reply=reply if isinstance(reply, str) else None,
                      attempts=_MAX_PROPOSAL_ATTEMPTS, model=model, seconds=time.monotonic() - started)


def _text(value: object) -> str | None:
    return value[:64] if isinstance(value, str) else None
