"""«Probarla conversando»: una conversacion de ensayo contra el agente con la campana servida (4.9, D-49.9).

Contrato: ``docs/contracts/agent-campaign-judge-v1.md`` §7. Con
``AUTOMATION_EXPERT_CAMPAIGN_CONVERSATION_ENABLED`` (ademas de ``AUTOMATION_EXPERT_JUDGE_ENABLED``),
el AE, ademas de pedir el juez de una campana validada, le pasa al bridge de 1 a 5 mensajes de un
lead inventado y el bridge los juega uno por uno contra el agente comercial **como lo recibiria con
la campana servida**: el SOUL comun, el conocimiento base del archivo y el bloque de la campana
(``agent_campaigns.load_campaign_segment``, el mismo que arma el catalogo vivo), por
``agent_rehearsal`` (OpenRouter con la clave del bridge, sin Hermes ni Chatwoot). Nadie recibe nada.

* ``POST .../campaigns/{id}/conversation``: la misma franja (09:00 a 21:00 de la instancia) y la
  misma corrida de la base que el juez (``start_agent_campaign_judge_run_v1``): campana validada, mas
  de 20 minutos antes de vencer, una sola corriendo por instancia y **el mismo tope de 3 por clave
  por dia, compartido con el juez** (D-49.10: los dos gastan el mismo modelo pago; D14). Contesta 202
  y corre despues.
* ``GET .../campaigns/{id}/conversation``: la ultima conversacion de ensayo, con la transcripcion y el
  veredicto.

D-49.11 (sin migracion): la corrida queda en la tabla del juez (``agent_campaign_judge_runs``,
20261126500000) y se distingue por ``result.tipo = "conversacion"``. La lectura del juez devuelve la
ultima corrida de cualquiera de los dos; por eso, con este flag, la ruta del juez suma ``tipo`` y esta
dice ``ultima_corrida: "juez"`` si la ultima no fue una conversacion. Una abandonada (sin resultado)
no se sabe de cual fue: se muestra en las dos.

D-49.12 (el veredicto): ``limpio`` si el bridge le habria mandado a la persona una respuesta a cada
mensaje; ``con_preguntas`` si en algun mensaje la propuesta no pasaba las reglas del bridge despues
del reintento (el turno real habria quedado ``failed`` y derivado). Que el agente derive o
descalifique no es una falla: la conversacion termina ahi (lo que sigue queda como ``no_llego``) y el
duenio lo lee. El contenido no lo juzga nadie: para eso esta el juez.

D-49.13: una campana con fases conversa con su primera pieza (la primera fase); el resultado dice
cual. El cupon y el enlace: el contexto dice que el bridge pone el enlace (``payment_link_action``)
y ``coupon_applied`` es falso (un lead que llego solo).

Los logs llevan la ruta, el id y el codigo; nunca los mensajes ni las respuestas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from . import agent_rehearsal
from .campaign_judge_api import (
    _RESULT_STATUS,
    _WORD,
    CONTRACT,
    CONTRACT_VERSION,
    DAILY_LIMIT,
    WINDOW_END,
    WINDOW_START,
    JudgeRunStore,
    _campaign_id,
    day_start,
    in_window,
)
from .flow_change_api import (
    PREFIX,
    EntryRefused,
    StoreRejected,
    StoreUnavailable,
    _body,
    _check_keys,
    _requested_by,
    looks_like_identifier,
)

__all__ = [
    "FLAG",
    "KIND",
    "ConversationJob",
    "check_settings",
    "default_runner",
    "register_campaign_conversation_routes",
    "run_conversation",
]

logger = logging.getLogger(__name__)

FLAG = "AUTOMATION_EXPERT_CAMPAIGN_CONVERSATION_ENABLED"
KIND = "conversacion"
JUDGE_KIND = "juez"
MAX_MESSAGES = 5
MAX_MESSAGE_CHARS = 300
# La columna admite 16384 bytes (20261126500000): la respuesta del agente se recorta para que entre.
_MAX_REPLY_SHOWN = 800
_MAX_RESULT_BYTES = 15_000
_STOPS = frozenset({"handoff", "disqualified"})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag: el juez prendido (su SOUL, su base y su clave) y el modelo del agente."""
    if not getattr(settings, "automation_expert_campaign_conversation_enabled", False):
        return
    if not getattr(settings, "automation_expert_judge_enabled", False):
        raise ValueError(f"{FLAG} requires AUTOMATION_EXPERT_JUDGE_ENABLED")
    path = getattr(settings, "commercial_ally_manifest_path", None)
    if agent_rehearsal.agent_model(Path(path).parent if path is not None else None) is None:
        raise ValueError(f"{FLAG} requires {agent_rehearsal.MODEL_ENV} or hermes.toml of the instance")


@dataclass(frozen=True)
class ConversationJob:
    """Con que se juega la conversacion: la base (solo para leer la campana), la instancia, el SOUL y el modelo."""

    base_url: str
    service_role_key: str
    context: Any  # agent_campaigns.CampaignContext
    soul_path: Path
    ask: agent_rehearsal.Ask


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Que entre en la columna: se acortan las respuestas del agente, de la mas larga a la mas corta."""
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_RESULT_BYTES:
        longest = max((turn for turn in result["turnos"] if turn.get("agente")),
                      key=lambda turn: len(turn["agente"]), default=None)
        if longest is None or len(longest["agente"]) <= 100:
            break
        longest["agente"] = longest["agente"][: len(longest["agente"]) * 2 // 3] + " […]"
    return result


def run_conversation(job: ConversationJob, campaign_id: str,
                     messages: Sequence[str]) -> tuple[str, dict[str, Any] | None, str | None]:
    """La conversacion, sincrona (va en un hilo). Devuelve ``(estado, resultado, error_code)``."""
    from .agent_campaigns import campaign_pieces, has_pieces, load_campaign_segment, parse_campaign
    from .campaign_check import _Store
    from .segment_check import SegmentCheckError
    from .segment_knowledge import SegmentError

    manifest = job.context.manifest
    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    base: dict[str, Any] = {"tipo": KIND, "mensajes": len(messages)}
    try:
        soul = job.soul_path.read_text(encoding="utf-8")
        row = _Store(job.base_url, job.service_role_key).call(
            "operator_get_agent_campaign_v1", {"p_instance": instance, "p_campaign_id": campaign_id})
        if row.get("status") != "validada":
            return "error", base, "agent_campaign_conversation_not_validated"
        draft = parse_campaign(row.get("draft"), phases=True)
        piece = campaign_pieces(draft)[0] if has_pieces(draft) else None
        segment = load_campaign_segment(piece.draft if piece is not None else draft,
                                        version=int(row["segment_version"]), context=job.context)
    except (OSError, UnicodeError):
        return "error", base, "agent_campaign_conversation_soul_unreadable"
    except (SegmentCheckError, httpx.HTTPError):
        return "error", base, "agent_campaign_conversation_store_unavailable"
    except (SegmentError, KeyError, TypeError, ValueError):
        return "error", base, "agent_campaign_conversation_check_failed"
    systems = (job.context.base.render(), segment.rendered)
    keys = frozenset(product.key for product in getattr(manifest, "products", ()) or ())
    history: list[tuple[str, str]] = []
    turns: list[dict[str, Any]] = []
    model: str | None = None
    rejected = 0
    stopped: str | None = None
    for index, text in enumerate(messages):
        if stopped is not None:
            turns.append({"lead": text, "agente": None, "estado": "no_llego"})
            continue
        history.append(("prospect", text))
        try:
            turn = agent_rehearsal.rehearse_turn(job.ask, soul=soul, systems=systems,
                                                 context=agent_rehearsal.build_context(history), product_keys=keys)
        except agent_rehearsal.RehearsalError:
            turns.append({"lead": text, "agente": None, "estado": "sin_respuesta_del_modelo"})
            turns.extend({"lead": rest, "agente": None, "estado": "no_llego"} for rest in messages[index + 1:])
            result = _fit({**base, "turnos": turns, "modelo": model, "pieza": piece.key if piece else None,
                           "segmento_sha256": segment.sha256[:12]})
            return "error", result, "agent_campaign_conversation_model_unavailable"
        model = turn.model or model
        shown = {"lead": text, "agente": (turn.reply or "")[:_MAX_REPLY_SHOWN] or None,
                 "decision": turn.decision, "reason_code": turn.reason_code, "intentos": turn.attempts,
                 "estado": "respondio" if turn.valid else "el_bridge_no_lo_mandaria"}
        if not turn.valid:
            rejected += 1
            shown["rechazo"] = turn.rejection
            # El turno real habria quedado failed y derivado: la conversacion no sigue.
            stopped = "derivada_por_falla"
        else:
            history.append(("assistant", turn.reply or ""))
            if turn.decision in _STOPS:
                stopped = str(turn.decision)
        turns.append(shown)
    result = _fit({**base, "veredicto": "limpio" if rejected == 0 else "con_preguntas", "turnos": turns,
                   "rechazados": rejected, "termino_en": stopped, "modelo": model,
                   "pieza": piece.key if piece is not None else None, "segmento_sha256": segment.sha256[:12]})
    return ("limpio" if rejected == 0 else "con_preguntas"), result, None


def _messages(body: Mapping[str, Any]) -> list[str]:
    raw = body.get("mensajes")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MESSAGES:
        raise EntryRefused("invalid_body", {"field": "mensajes", "problem": f"list_of_1_to_{MAX_MESSAGES}"})
    out = []
    for item in raw:
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= MAX_MESSAGE_CHARS:
            raise EntryRefused("invalid_body", {"field": "mensajes", "problem": f"text_1_to_{MAX_MESSAGE_CHARS}"})
        if looks_like_identifier(item):
            # Un lead inventado: un telefono o un mail no tienen que estar.
            raise EntryRefused("identifier_not_allowed", {"field": "mensajes", "problem": "phone_or_mail"})
        out.append(item.strip())
    return out


def kind_of(run: Mapping[str, Any] | None) -> str | None:
    """De cual es una corrida de la tabla del juez: ``conversacion``, ``juez`` o None (abandonada, sin resultado)."""
    if run is None:
        return None
    result = run.get("result")
    if isinstance(result, Mapping):
        return KIND if result.get("tipo") == KIND else JUDGE_KIND
    return None


def register_campaign_conversation_routes(
    app: FastAPI, *, token: str, store: JudgeRunStore, manifest: Any,
    run: Callable[[str, Sequence[str]], tuple[str, dict[str, Any] | None, str | None]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC), git_sha: str | None = None,
    daily_limit: int = DAILY_LIMIT,
) -> None:
    """Las dos rutas, detras del flag (lo mira quien llama: apagado no existen)."""
    import hmac

    from zoneinfo import ZoneInfo

    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    zone = ZoneInfo(manifest.time_zone)
    sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"
    tasks: set[asyncio.Task[None]] = set()
    app.state.campaign_conversation_tasks = tasks

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=401, detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": clock().isoformat().replace("+00:00", "Z"), "bridge_git_sha": sha,
                "data": dict(data)}

    def window() -> dict[str, str]:
        return {"desde": WINDOW_START.strftime("%H:%M"), "hasta": WINDOW_END.strftime("%H:%M"),
                "zona": manifest.time_zone}

    def failure(error: Exception, route: str, campaign_id: str) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        else:
            code, http_status = getattr(error, "code", "automation_expert_change_unavailable"), 503
        logger.info("campaign_conversation route=%s campaign=%s status=%s code=%s", route, campaign_id,
                    http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    async def finish(run_id: str, campaign_id: str, messages: list[str]) -> None:
        try:
            state, result, error_code = await asyncio.to_thread(run, campaign_id, messages)
        except Exception as error:  # noqa: BLE001 - lo que no se espera queda como error de la corrida
            logger.warning("campaign_conversation_failed campaign=%s error_type=%s", campaign_id,
                           type(error).__name__)
            state, result, error_code = "error", None, "agent_campaign_conversation_failed"
        # Siempre con el tipo, tambien un error: asi la lectura sabe de cual es (D-49.11).
        result = {**(result or {}), "tipo": KIND}
        try:
            await store.finish_run(instance, run_id=run_id, status=state, result=result, error_code=error_code)
        except (StoreUnavailable, StoreRejected) as error:
            logger.warning("campaign_conversation_finish_failed campaign=%s run=%s code=%s", campaign_id, run_id,
                           getattr(error, "code", "unknown"))
            return
        logger.info("campaign_conversation_finished campaign=%s run=%s status=%s code=%s", campaign_id, run_id,
                    state, error_code)

    @app.post(f"{PREFIX}/campaigns/{{campaign_id}}/conversation", status_code=202)
    async def start_conversation(campaign_id: str, request: Request,
                                 authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _campaign_id(campaign_id)
        body = _check_keys(await _body(request), required={"requested_by", "mensajes"},
                           allowed={"requested_by", "mensajes"})
        requested_by = _requested_by(body)
        messages = _messages(body)
        now = clock()
        if not in_window(now, zone):
            logger.info("campaign_conversation route=start campaign=%s status=409 "
                        "code=agent_campaign_judge_outside_hours", checked)
            raise HTTPException(status_code=409, detail="agent_campaign_judge_outside_hours")
        try:
            started = await store.start_run(instance, campaign_id=checked, requested_by=requested_by,
                                            day_start=day_start(now, zone), daily_limit=daily_limit)
        except (StoreUnavailable, StoreRejected) as error:
            raise failure(error, "start", checked) from None
        if started.get("ok") is not True:
            code = started.get("reason_code")
            if isinstance(code, str) and code in _RESULT_STATUS:
                logger.info("campaign_conversation route=start campaign=%s status=409 code=%s", checked, code)
                raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
            raise failure(StoreUnavailable(), "start", checked)
        run_id = started.get("run_id")
        if not isinstance(run_id, str):
            raise failure(StoreUnavailable(), "start", checked)
        task = asyncio.create_task(finish(run_id, checked, messages))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        logger.info("campaign_conversation route=start campaign=%s run=%s status=202 messages=%s", checked, run_id,
                    len(messages))
        return envelope({"campaign_id": checked, "run_id": run_id, "tipo": KIND, "status": "corriendo",
                         "resultado": _WORD["corriendo"], "runs_today": started.get("runs_today"),
                         "daily_limit": daily_limit, "window": window(), "tarda": "uno o dos minutos"})

    @app.get(f"{PREFIX}/campaigns/{{campaign_id}}/conversation")
    async def read_conversation(campaign_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _campaign_id(campaign_id)
        now = clock()
        try:
            read = await store.get_run(instance, campaign_id=checked, day_start=day_start(now, zone))
        except (StoreUnavailable, StoreRejected) as error:
            raise failure(error, "read", checked) from None
        last = read.get("last_run")
        runs_today = read.get("runs_today")
        if (last is not None and not isinstance(last, Mapping)) or type(runs_today) is not int:
            raise failure(StoreUnavailable(), "read", checked)
        kind = kind_of(last)
        shown = None
        if last is not None and kind != JUDGE_KIND:
            state = last.get("status")
            shown = {"run_id": last.get("run_id"), "status": state, "resultado": _WORD.get(state, "ERROR"),
                     "error_code": last.get("error_code"), "result": last.get("result"),
                     "started_at": last.get("started_at"), "finished_at": last.get("finished_at")}
        running = isinstance(last, Mapping) and last.get("status") == "corriendo"
        logger.info("campaign_conversation route=read campaign=%s status=200", checked)
        return envelope({"campaign_id": checked, "last_conversation": shown,
                         "ultima_corrida": kind if kind is not None else ("desconocida" if last else None),
                         "runs_today": runs_today, "daily_limit": daily_limit, "window": window(),
                         "can_start_now": in_window(now, zone) and runs_today < daily_limit and not running})


def default_runner(settings: Any, context: Any) -> Callable[[str, Sequence[str]], tuple[str, dict[str, Any] | None,
                                                                                      str | None]]:
    """La conversacion de verdad, armada con lo que ``check_settings`` (y el del juez) ya exigieron."""
    path = Path(settings.commercial_ally_manifest_path)
    model = agent_rehearsal.agent_model(path.parent)
    assert model is not None
    job = ConversationJob(base_url=settings.supabase_base_url, service_role_key=settings.supabase_service_role_key,
                          context=context, soul_path=Path(settings.automation_expert_judge_soul_path),
                          ask=agent_rehearsal.openrouter_ask(model=model, api_key=settings.openrouter_api_key))
    return lambda campaign_id, messages: run_conversation(job, campaign_id, messages)
