"""El juez de una campana, pedido por el Automation Expert (agent-campaign-judge-v1, 4.9, F-163).

Hoy el control de calidad lo corre el operador con ``python -m bridge.campaign_check``. Con
``AUTOMATION_EXPERT_JUDGE_ENABLED`` lo pide el AE y lo corre el bridge **en segundo plano**, con la
misma funcion (``campaign_check.run_check``, ``--criterio frenos``, 3 corridas), sin shell:

* ``POST .../campaigns/{id}/judge``: mira la franja (09:00 a 21:00 de la zona de la instancia), abre
  la corrida en la base (campana validada, mas de 20 minutos antes de vencer, una sola corriendo por
  instancia y hasta 3 por clave de campana por dia) y contesta 202 enseguida. El juez corre despues.
* ``GET .../campaigns/{id}/judge``: la ultima corrida (``CORRIENDO``, ``LIMPIO``, ``CON-PREGUNTAS`` o
  ``ERROR``, con las preguntas) y cuantas lleva hoy.

Con el flag apagado las rutas no existen y nada de este modulo llama al modelo: el juez de
OpenRouter se arma recien adentro de la corrida, que solo nace de una ruta. El log lleva la ruta, el
id y el codigo; nunca la clave ni el texto de la campana.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status

from .flow_change_api import (
    PREFIX,
    FlowChangeStore,
    StoreRejected,
    StoreUnavailable,
    _body,
    _check_keys,
    _requested_by,
)

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "DAILY_LIMIT",
    "FLAG",
    "JudgeJob",
    "JudgeRunStore",
    "check_settings",
    "day_start",
    "default_runner",
    "in_window",
    "outcome",
    "register_campaign_judge_routes",
    "run_judge",
]

logger = logging.getLogger(__name__)

CONTRACT = "agent-campaign-judge-v1"
CONTRACT_VERSION = "1.0.0"
FLAG = "AUTOMATION_EXPERT_JUDGE_ENABLED"
SOUL_ENV = "AUTOMATION_EXPERT_JUDGE_SOUL_PATH"
# D14 del mapa Z1: 3 corridas por campana por dia, de 09:00 a 21:00 de la instancia.
DAILY_LIMIT = 3
WINDOW_START = time(9, 0)
WINDOW_END = time(21, 0)
CRITERIO = "frenos"
# Lo que el AE le cuenta al duenio: hasta diez preguntas, recortadas (la columna admite 16384 bytes).
_MAX_QUESTIONS = 10
_MAX_QUESTION_CHARS = 300
_MAX_FIELD_CHARS = 120
_MAX_NOTICES = 5
_MAX_NOTICE_CHARS = 200
_CODE = re.compile(r"[a-z][a-z0-9_]{0,44}")
_WORD = {"corriendo": "CORRIENDO", "limpio": "LIMPIO", "con_preguntas": "CON-PREGUNTAS", "error": "ERROR"}
_RESULT_STATUS = {
    "agent_campaign_expired": 409,
    "agent_campaign_not_validated": 409,
    "agent_campaign_judge_expires_soon": 409,
    "agent_campaign_judge_running": 409,
    "agent_campaign_judge_daily_limit": 409,
}


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar: la campana, la base con service_role, el SOUL y la clave del modelo."""
    if not getattr(settings, "automation_expert_judge_enabled", False):
        return
    if not getattr(settings, "agent_campaigns_enabled", False):
        raise ValueError(f"{FLAG} requires AGENT_CAMPAIGNS_ENABLED")
    if getattr(settings, "instance_manifest", None) is None \
            or getattr(settings, "commercial_ally_manifest_path", None) is None:
        raise ValueError(f"{FLAG} requires an instance manifest")
    if not getattr(settings, "supabase_base_url", None) or not getattr(settings, "supabase_service_role_key", None):
        raise ValueError(f"{FLAG} requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    if not getattr(settings, "openrouter_api_key", None):
        raise ValueError(f"{FLAG} requires OPENROUTER_API_KEY")
    soul = getattr(settings, "automation_expert_judge_soul_path", None)
    if soul is None or not Path(soul).is_file():
        raise ValueError(f"{FLAG} requires {SOUL_ENV} (the common SOUL of the agent, as a file)")


class JudgeRunStore(FlowChangeStore):
    """Las tres funciones de 20261126500000, con la clave de service_role del bridge."""

    async def start_run(self, instance: Mapping[str, str], *, campaign_id: str, requested_by: str,
                        day_start: datetime, daily_limit: int) -> dict[str, Any]:
        return await self._rpc("start_agent_campaign_judge_run_v1", {
            "p_instance": instance, "p_campaign_id": campaign_id, "p_requested_by_ref": requested_by,
            "p_day_start": day_start.isoformat(), "p_daily_limit": daily_limit})

    async def finish_run(self, instance: Mapping[str, str], *, run_id: str, status: str,
                         result: Mapping[str, Any] | None, error_code: str | None) -> dict[str, Any]:
        return await self._rpc("finish_agent_campaign_judge_run_v1", {
            "p_instance": instance, "p_run_id": run_id, "p_status": status, "p_result": result,
            "p_error_code": error_code})

    async def get_run(self, instance: Mapping[str, str], *, campaign_id: str, day_start: datetime) -> dict[str, Any]:
        return await self._rpc("get_agent_campaign_judge_run_v1", {
            "p_instance": instance, "p_campaign_id": campaign_id, "p_day_start": day_start.isoformat()})


@dataclass(frozen=True)
class JudgeJob:
    """Con que corre el juez: lo mismo que el comando del operador (``juez.sh`` de e673)."""

    base_url: str
    service_role_key: str
    manifest: Any
    instance_dir: Path
    soul_path: Path
    openrouter_api_key: str


def _clip(text: object, limit: int) -> str:
    return str(text or "")[:limit]


def outcome(report: Any, saved: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
    """El estado, el resultado y el codigo de error de una corrida, como los cuenta el comando.

    ``limpio`` si el juez dio limpio y la base lo anoto; ``con_preguntas`` si quedaron preguntas; ``error`` si
    no hubo veredicto (el comando sale con 1, pero no hay preguntas que contar) o si la base no lo anoto."""
    from .campaign_check import report_for_row
    from .campaign_gate import CRITERIO as GATE_NAME

    kept_clean = saved.get("reason_code") == "agent_campaign_check_not_clean"
    if saved.get("ok") is not True and not (kept_clean and not report.clean):
        code = saved.get("reason_code")
        suffix = code if isinstance(code, str) and _CODE.fullmatch(code) else "unknown"
        return "error", None, f"agent_campaign_judge_not_recorded:{suffix}"[:80]
    row = report_for_row(report, criterio=GATE_NAME)
    result = {
        "veredicto": row["veredicto"], "abiertos": row["abiertos"],
        "corridas_validas": row["corridas_validas"], "corridas_pedidas": row["corridas_pedidas"],
        "criterio": row.get("criterio"), "avisos_total": row.get("avisos_total", 0),
        "preguntas": [{"numero": item.get("numero"), "tipo": item.get("tipo"), "razon": item.get("razon"),
                       "campo": _clip(item.get("campo"), _MAX_FIELD_CHARS),
                       "pregunta": _clip(item.get("pregunta"), _MAX_QUESTION_CHARS)}
                      for item in row["items"][:_MAX_QUESTIONS]],
        "avisos": [{"numero": item.get("numero"), "tipo": item.get("tipo"),
                    "pregunta": _clip(item.get("pregunta"), _MAX_NOTICE_CHARS)}
                   for item in row.get("avisos", [])[:_MAX_NOTICES]],
    }
    if report.clean:
        return "limpio", result, None
    if row["veredicto"] == "sin_veredicto":
        # D-49.6: sin veredicto no hay preguntas que contar: es un ERROR (se corre otra vez), con lo que dio.
        return "error", result, "agent_campaign_judge_no_verdict"
    return "con_preguntas", result, None


def run_judge(job: JudgeJob, campaign_id: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Una corrida, sincrona (va en un hilo): ``campaign_check.run_check`` con ``--criterio frenos``.

    El juez de OpenRouter se arma aca adentro, con la clave del bridge: no hay otro camino que lo llame."""
    from .campaign_check import _Store, run_check
    from .segment_check import DEFAULT_MODEL, DEFAULT_RUNS, JudgeError, SegmentCheckError, openrouter_judge

    try:
        judge = openrouter_judge(model=os.environ.get("SEGMENT_CHECK_MODEL", DEFAULT_MODEL),
                                 api_key=job.openrouter_api_key)
        report, saved = run_check(_Store(job.base_url, job.service_role_key), manifest=job.manifest,
                                  instance_dir=job.instance_dir, campaign_id=campaign_id, judge=judge,
                                  soul_path=job.soul_path, runs=DEFAULT_RUNS, criterio=CRITERIO)
    except JudgeError:
        return "error", None, "agent_campaign_judge_model_unavailable"
    except SegmentCheckError:
        return "error", None, "agent_campaign_judge_check_failed"
    except httpx.HTTPError:
        return "error", None, "agent_campaign_judge_store_unavailable"
    except (KeyError, ValueError):
        return "error", None, "agent_campaign_judge_input_invalid"
    return outcome(report, saved)


def _campaign_id(value: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_campaign_id") from None


def day_start(now: datetime, zone: ZoneInfo) -> datetime:
    """El comienzo del dia de ``now`` en la zona de la instancia, en UTC."""
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(0, 0), tzinfo=zone).astimezone(UTC)


def in_window(now: datetime, zone: ZoneInfo) -> bool:
    local = now.astimezone(zone).time()
    return WINDOW_START <= local < WINDOW_END


def register_campaign_judge_routes(
    app: FastAPI, *, token: str, store: JudgeRunStore, manifest: Any,
    run: Callable[[str], tuple[str, dict[str, Any] | None, str | None]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC), git_sha: str | None = None,
    daily_limit: int = DAILY_LIMIT,
) -> None:
    """Las dos rutas, detras del flag (lo mira quien llama: apagado no existen).

    ``run`` es la corrida sincrona (``run_judge`` con su ``JudgeJob``); va en un hilo y su resultado se
    anota con ``finish_run``. Si el bridge se reinicia en el medio, la base la da por abandonada a los 30
    minutos."""
    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    zone = ZoneInfo(manifest.time_zone)
    sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"
    tasks: set[asyncio.Task[None]] = set()
    app.state.campaign_judge_tasks = tasks

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
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
        logger.info("campaign_judge route=%s campaign=%s status=%s code=%s", route, campaign_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    async def finish(run_id: str, campaign_id: str) -> None:
        try:
            state, result, error_code = await asyncio.to_thread(run, campaign_id)
        except Exception as error:  # noqa: BLE001 - lo que no se espera queda como error de la corrida
            logger.warning("campaign_judge_run_failed campaign=%s error_type=%s", campaign_id, type(error).__name__)
            state, result, error_code = "error", None, "agent_campaign_judge_failed"
        try:
            await store.finish_run(instance, run_id=run_id, status=state, result=result, error_code=error_code)
        except (StoreUnavailable, StoreRejected) as error:
            logger.warning("campaign_judge_finish_failed campaign=%s run=%s code=%s", campaign_id, run_id,
                           getattr(error, "code", "unknown"))
            return
        logger.info("campaign_judge_finished campaign=%s run=%s status=%s code=%s", campaign_id, run_id, state,
                    error_code)

    @app.post(f"{PREFIX}/campaigns/{{campaign_id}}/judge", status_code=202)
    async def start_judge(campaign_id: str, request: Request,
                          authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _campaign_id(campaign_id)
        body = _check_keys(await _body(request), required={"requested_by"}, allowed={"requested_by"})
        requested_by = _requested_by(body)
        now = clock()
        if not in_window(now, zone):
            # Fuera de la franja no se le pide nada a la base ni al modelo.
            logger.info("campaign_judge route=start campaign=%s status=409 code=agent_campaign_judge_outside_hours",
                        checked)
            raise HTTPException(status_code=409, detail="agent_campaign_judge_outside_hours")
        try:
            started = await store.start_run(instance, campaign_id=checked, requested_by=requested_by,
                                            day_start=day_start(now, zone), daily_limit=daily_limit)
        except (StoreUnavailable, StoreRejected) as error:
            raise failure(error, "start", checked) from None
        if started.get("ok") is not True:
            code = started.get("reason_code")
            if isinstance(code, str) and code in _RESULT_STATUS:
                logger.info("campaign_judge route=start campaign=%s status=409 code=%s", checked, code)
                raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
            raise failure(StoreUnavailable(), "start", checked)
        run_id = started.get("run_id")
        if not isinstance(run_id, str):
            raise failure(StoreUnavailable(), "start", checked)
        task = asyncio.create_task(finish(run_id, checked))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        logger.info("campaign_judge route=start campaign=%s run=%s status=202 runs_today=%s", checked, run_id,
                    started.get("runs_today"))
        return envelope({"campaign_id": checked, "run_id": run_id, "status": "corriendo",
                         "resultado": _WORD["corriendo"], "runs_today": started.get("runs_today"),
                         "daily_limit": daily_limit, "window": window(),
                         "tarda": "de 6 a 17 minutos"})

    @app.get(f"{PREFIX}/campaigns/{{campaign_id}}/judge")
    async def read_judge(campaign_id: str, authorization: str | None = Header(default=None)) -> Any:
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
        shown = None
        if last is not None:
            state = last.get("status")
            shown = {"run_id": last.get("run_id"), "status": state, "resultado": _WORD.get(state, "ERROR"),
                     "error_code": last.get("error_code"), "result": last.get("result"),
                     "started_at": last.get("started_at"), "finished_at": last.get("finished_at")}
        logger.info("campaign_judge route=read campaign=%s status=200", checked)
        return envelope({"campaign_id": checked, "last_run": shown, "runs_today": runs_today,
                         "daily_limit": daily_limit, "window": window(),
                         "can_start_now": in_window(now, zone) and runs_today < daily_limit
                         and not (shown is not None and shown["status"] == "corriendo")})


def default_runner(settings: Any) -> Callable[[str], tuple[str, dict[str, Any] | None, str | None]]:
    """La corrida de verdad, armada con lo que ``check_settings`` ya exigio."""
    manifest_path = Path(settings.commercial_ally_manifest_path)
    job = JudgeJob(base_url=settings.supabase_base_url, service_role_key=settings.supabase_service_role_key,
                   manifest=settings.instance_manifest, instance_dir=manifest_path.parent,
                   soul_path=Path(settings.automation_expert_judge_soul_path),
                   openrouter_api_key=settings.openrouter_api_key)
    return lambda campaign_id: run_judge(job, campaign_id)

