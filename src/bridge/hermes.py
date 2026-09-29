"""Hermes API client for non-sending shadow evaluations."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from bridge.agent_provenance import AgentTurn, build_turn


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


def _parse_agent_proposal(content: object) -> dict[str, object] | None:
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
            proposal = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(proposal, dict):
            return proposal
    return None


def _is_valid_proposal(proposal: dict[str, object]) -> bool:
    if set(proposal) != {
        "decision",
        "qualification_status",
        "reason_code",
        "reply",
        "captured_fields",
        "missing_fields",
    }:
        return False

    decision = proposal["decision"]
    if not isinstance(decision, str) or decision not in _DECISION_STATUSES:
        return False
    if proposal["qualification_status"] != _DECISION_STATUSES.get(decision):
        return False

    reason_code = proposal["reason_code"]
    if not isinstance(reason_code, str) or re.fullmatch(
        r"[a-z0-9_]{1,64}", reason_code
    ) is None:
        return False

    reply = proposal["reply"]
    if not isinstance(reply, str) or not reply or len(reply) > 1000:
        return False
    if reply.count("?") > 1:
        return False
    if decision == "send_payment_link" and any(
        marker in reply.lower() for marker in ("http://", "https://", "www.")
    ):
        return False

    captured = proposal["captured_fields"]
    if not isinstance(captured, dict) or set(captured) != _CAPTURED_FIELDS:
        return False
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
        return False
    company_size = captured["company_size"]
    if company_size is not None and (
        not isinstance(company_size, int)
        or isinstance(company_size, bool)
        or company_size <= 0
    ):
        return False
    if any(
        value is not None and not isinstance(value, bool)
        for field, value in captured.items()
        if field in _BOOLEAN_CAPTURED_FIELDS
    ):
        return False

    missing = proposal["missing_fields"]
    if not isinstance(missing, list):
        return False
    if not all(isinstance(field, str) for field in missing):
        return False
    if len(missing) != len(set(missing)):
        return False
    return all(
        isinstance(field, str)
        and field in _CAPTURED_FIELDS
        and captured[field] is None
        for field in missing
    )


class TurnProvenanceRecorder(Protocol):
    """Quien sabe grabar la procedencia de un turno.

    Es un protocolo y no una dependencia concreta para que este modulo no
    aprenda de Supabase: el procesador sabe QUE paso en el turno, no donde
    se anota. El cableado vive en `bridge.app`.
    """

    async def record_agent_turn(self, *, turn: AgentTurn) -> None:
        ...  # pragma: no cover - protocolo


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
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
        if not isinstance(proposal, dict) or not _is_valid_proposal(proposal):
            return None
        return proposal

    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
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
                )
            )
        except Exception:  # noqa: BLE001 - la propuesta manda
            pass

    async def _request_and_persist(
        self, *, digest: str, context: dict[str, object], delivery_id: str
    ) -> None:
        # Lo ultimo que Hermes dijo que contesto. Se arrastra fuera del
        # bucle porque el punto terminal de salida invalida esta despues.
        model_answered: str | None = None
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
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        context, ensure_ascii=False
                                    ),
                                },
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
            else:
                proposal = _parse_agent_proposal(proposal_text)

            if isinstance(proposal, dict) and _is_valid_proposal(proposal):
                self._persist_result(
                    digest=digest,
                    result=self._attempted(
                        {
                            "status": "completed",
                            "delivery_id_hash": digest,
                            "proposal": proposal,
                        },
                        attempt=attempt,
                    ),
                )
                await self._record_turn(
                    delivery_id=delivery_id,
                    context=context,
                    attempt=attempt,
                    outcome="completed",
                    model_answered=model_answered,
                )
                return

        self._persist_result(
            digest=digest,
            result=self._attempted(
                {
                    "status": "failed",
                    "delivery_id_hash": digest,
                    "reason": "invalid_agent_output",
                },
                attempt=_MAX_PROPOSAL_ATTEMPTS,
            ),
        )
        await self._record_turn(
            delivery_id=delivery_id,
            context=context,
            attempt=_MAX_PROPOSAL_ATTEMPTS,
            outcome="failed",
            model_answered=model_answered,
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

    @staticmethod
    def _is_terminal_result(path: Path, *, digest: str) -> bool:
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
            and _is_valid_proposal(proposal)
        )
