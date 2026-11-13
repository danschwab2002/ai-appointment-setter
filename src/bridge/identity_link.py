"""El correlador que decide solo (identity-link-decider-v1, E-536).

Cuando el correlador de Hotmart deja un carrito en ``conflict /
email_phone_conflict`` (la persona dejo un mail o un telefono en el formulario
y compro con otro), esta pieza lo resuelve en minutos y sin el equipo, en una
de tres ramas:

* **ata**: la base escribe la atadura y vuelve a encolar el carrito, que sigue
  su flujo de hoy (legado o nativo) con sus toques, su franja, sus topes, su
  baja y su compra. Esta pieza no manda nada por su cuenta en esta rama.
* **pregunta** (bloque B, con su flag): una plantilla unica al telefono del
  permiso, con dos respuestas rapidas («Sí, fui yo» y «No más mensajes»).
* **nada**: solo se registra.

El orden es fijo (D1): primero los frenos de la base, que no llaman a nadie;
despues la regla para lo claro (R1, el mismo telefono; R2, el mismo mail y el
nombre parecido al 90 % o mas); el modelo solo para lo que queda. La invariante
(D2): a que telefono se escribe lo fija la base y lo vuelve a exigir el chequeo
del permiso antes de cada envio; el modelo solo dice si es la misma persona.

El modelo ve rasgos (si/no, 0-100, el prefijo del pais), nunca un mail, un
telefono ni un nombre. Su texto no se guarda: solo la decision, la confianza y
los tokens de una lista cerrada. Una respuesta con otra forma, un token fuera
de la lista o un «ata» sin un rasgo fuerte se degradan a «nada»; nunca se falla
hacia «ata».
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
import hashlib
import json
import logging
import re
from typing import Any, Protocol
import uuid
from zoneinfo import ZoneInfo

import httpx

from bridge.correlation_preresolution import (
    _validate_base_url,
    parse_preresolution_proposal,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "identity-link-v1"
MODES = ("off", "sombra", "vivo")
MAX_MODEL_ATTEMPTS = 3
R2_NAME_SIMILARITY = 90
# Los motivos de una rama «nada» que pone el bridge (los frenos los pone la base).
MODEL_INVALID = "modelo_respuesta_invalida"
MODEL_WITHOUT_STRONG_TRAIT = "modelo_sin_rasgo_fuerte"
MODEL_UNAVAILABLE = "modelo_no_disponible"
MODEL_DIFFERENT_PERSON = "modelo_otra_persona"
MODEL_LOW_CONFIDENCE = "modelo_confianza_baja"
WOULD_HAVE_ASKED = "habria_preguntado"
DESTINATION_WITHOUT_PERMISSION = "destino_sin_permiso"
EVIDENCE_INVALID = "evidencia_invalida"
DECISION_REFUSED = "decision_rechazada"
GUARD_REASONS = frozenset(
    {
        "fuera_de_alcance",
        "sin_permiso",
        "baja_previa",
        "ya_compro",
        "intencion_no_viva",
        "carrito_viejo",
        "destino_sin_permiso",
        "contacto_ambiguo",
    }
)
# Los tokens que el modelo puede citar. Los tres primeros son los rasgos fuertes.
STRONG_REASON_CODES = frozenset(
    {"phone_canonical_equal", "email_exact_equal", "phone_national_equal_country_differs"}
)
REASON_CODES = STRONG_REASON_CODES | frozenset(
    {
        "email_same_local_other_domain",
        "email_same_domain_other_local",
        "email_one_letter_off",
        "email_differs",
        "phone_last7_equal",
        "phone_differs",
        "name_equal",
        "name_similar",
        "name_differs",
        "first_name_equal",
        "same_offer",
        "other_offer",
        "several_intents",
        "prior_conversation",
        "close_in_time",
        "far_in_time",
    }
)
_MODEL_DECISIONS = frozenset({"same_person", "unsure", "different_person"})
_MODEL_CONFIDENCES = frozenset({"high", "medium", "low"})
_PROPOSAL_KEYS = frozenset({"decision", "confidence", "reason_codes"})
# Los rasgos que la base devuelve, con su tipo. Cualquier otra clave o tipo hace
# la evidencia invalida: nada personal puede entrar al contexto del modelo.
_BOOL_TRAITS = (
    "telefono_canonico_igual",
    "telefono_10_igual",
    "telefono_7_igual",
    "mail_igual",
    "dominio_igual",
    "usuario_igual",
    "primer_nombre_igual",
    "misma_oferta",
    "conversacion_previa_con_el_telefono_del_permiso",
    "selector_pais_sospechoso",
)
_PERCENT_TRAITS = ("parecido_usuario", "parecido_dominio", "parecido_nombre")
_COUNT_TRAITS = ("minutos_formulario_a_carrito", "otras_intenciones_7d")
_COUNTRIES = frozenset({"MX", "NANP", "AR", "otro"})
_TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
_SAFE_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")

SYSTEM_PROMPT_V1 = """You compare two records of a purchase attempt: a landing form and a checkout.
You only see derived traits (yes/no, 0-100 similarity scores, country prefixes, counts); never an email, a phone or a name.
Decide whether both records are the same person.
Return exactly one JSON object with exactly these keys and no others: decision, confidence, reason_codes.
decision must be exactly "same_person", "unsure" or "different_person".
confidence must be exactly "high", "medium" or "low".
reason_codes must be a JSON array (possibly empty) of unique tokens from this closed list:
""" + ", ".join(sorted(REASON_CODES)) + """.
Answer "same_person" with "high" confidence only when at least one of phone_canonical_equal, email_exact_equal or phone_national_equal_country_differs is true in the traits, and cite it.
When the traits conflict or are weak, answer "unsure". Never invent traits. No prose, no Markdown fences.
"""
SYSTEM_PROMPTS = {PROMPT_VERSION: SYSTEM_PROMPT_V1}


class IdentityLinkProviderError(RuntimeError):
    """El modelo no contesto o contesto algo que no es un JSON."""


@dataclass(frozen=True)
class IdentityLinkEvidence:
    """La evidencia congelada de un conflicto: el freno de la base y los rasgos."""

    guard_reason: str | None
    traits: Mapping[str, object]

    @classmethod
    def from_snapshot(cls, snapshot: object) -> IdentityLinkEvidence:
        if not isinstance(snapshot, Mapping):
            raise ValueError(EVIDENCE_INVALID)
        guard = snapshot.get("guard_reason")
        if guard is not None and guard not in GUARD_REASONS:
            raise ValueError(EVIDENCE_INVALID)
        raw = snapshot.get("rasgos")
        if not isinstance(raw, Mapping):
            raise ValueError(EVIDENCE_INVALID)
        if guard == "fuera_de_alcance":
            return cls(guard_reason=guard, traits={})
        expected = {
            "coincide_por",
            "pais_formulario",
            "pais_checkout",
            *_BOOL_TRAITS,
            *_PERCENT_TRAITS,
            *_COUNT_TRAITS,
        }
        if set(raw) != expected:
            raise ValueError(EVIDENCE_INVALID)
        if raw["coincide_por"] not in {"telefono", "mail"}:
            raise ValueError(EVIDENCE_INVALID)
        for key in ("pais_formulario", "pais_checkout"):
            if raw[key] not in _COUNTRIES:
                raise ValueError(EVIDENCE_INVALID)
        for key in _BOOL_TRAITS:
            if not isinstance(raw[key], bool):
                raise ValueError(EVIDENCE_INVALID)
        for key in _PERCENT_TRAITS:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(EVIDENCE_INVALID)
        for key in _COUNT_TRAITS:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(EVIDENCE_INVALID)
        return cls(guard_reason=guard, traits=dict(raw))

    def flag(self, key: str) -> bool:
        return self.traits.get(key) is True

    def to_model_context(self) -> dict[str, object]:
        return {"persona": "Persona 1", "rasgos": dict(sorted(self.traits.items()))}


@dataclass(frozen=True)
class IdentityLinkVerdict:
    """Lo que se le manda a la base: la rama y quien la decidio."""

    branch: str
    decided_by: str
    guard_reason: str | None = None
    model_decision: str | None = None
    model_confidence: str | None = None
    reason_codes: tuple[str, ...] = ()
    prompt_version: str | None = None
    destination_source: str | None = None

    def __post_init__(self) -> None:
        if self.branch not in {"ata", "pregunta", "nada"}:
            raise ValueError("invalid_identity_link_branch")
        if (self.branch == "nada") != (self.destination_source is None):
            raise ValueError("invalid_identity_link_destination")
        if self.branch == "ata" and self.guard_reason is not None:
            raise ValueError("invalid_identity_link_guard")


def _nothing(reason: str, *, decided_by: str = "freno", **model: Any) -> IdentityLinkVerdict:
    return IdentityLinkVerdict(branch="nada", decided_by=decided_by, guard_reason=reason, **model)


def _destination(evidence: IdentityLinkEvidence, *, country_code_fix_enabled: bool) -> str | None:
    """El destino que la base va a aceptar, o None si no hay uno con permiso.

    Siempre el telefono del permiso; con el +1 del selector del formulario (el
    formulario guardo 1 + los 10 digitos que el checkout trae con 52), el del
    checkout solo con D3 prendida y el mail igual. El +1 nunca es destino.
    """
    if evidence.flag("selector_pais_sospechoso"):
        if country_code_fix_enabled and evidence.flag("mail_igual"):
            return "intent_national_digits"
        return None
    return "intent_phone"


def apply_rules(
    evidence: IdentityLinkEvidence,
    *,
    rules_first: bool = True,
    country_code_fix_enabled: bool = False,
) -> IdentityLinkVerdict | None:
    """Los frenos y la regla (D1), sin el modelo. None: le toca al modelo."""
    if evidence.guard_reason is not None:
        return _nothing(evidence.guard_reason)
    destination = _destination(evidence, country_code_fix_enabled=country_code_fix_enabled)
    if destination is None:
        return _nothing(DESTINATION_WITHOUT_PERMISSION)
    if not rules_first:
        return None
    if evidence.flag("telefono_canonico_igual"):
        return IdentityLinkVerdict(branch="ata", decided_by="regla:R1", destination_source=destination)
    name = evidence.traits.get("parecido_nombre")
    if evidence.flag("mail_igual") and isinstance(name, int) and name >= R2_NAME_SIMILARITY:
        return IdentityLinkVerdict(branch="ata", decided_by="regla:R2", destination_source=destination)
    return None


def verdict_from_model(
    proposal: object,
    evidence: IdentityLinkEvidence,
    *,
    model_name: str,
    ask_enabled: bool,
    country_code_fix_enabled: bool = False,
    prompt_version: str = PROMPT_VERSION,
) -> IdentityLinkVerdict:
    """Del modelo a la rama. Toda salida mala termina en «nada», nunca en «ata»."""
    decided_by = f"modelo:{model_name}"
    common: dict[str, Any] = {"decided_by": decided_by, "prompt_version": prompt_version}
    if (
        not isinstance(proposal, Mapping)
        or set(proposal) != _PROPOSAL_KEYS
        or proposal["decision"] not in _MODEL_DECISIONS
        or proposal["confidence"] not in _MODEL_CONFIDENCES
        or not isinstance(proposal["reason_codes"], list)
        or len(proposal["reason_codes"]) > 20
        or len(set(map(str, proposal["reason_codes"]))) != len(proposal["reason_codes"])
        or any(not isinstance(code, str) or code not in REASON_CODES for code in proposal["reason_codes"])
    ):
        return _nothing(MODEL_INVALID, **common)
    decision = str(proposal["decision"])
    confidence = str(proposal["confidence"])
    codes = tuple(str(code) for code in proposal["reason_codes"])
    model = {**common, "model_decision": decision, "model_confidence": confidence, "reason_codes": codes}
    if decision == "different_person":
        return _nothing(MODEL_DIFFERENT_PERSON, **model)
    if confidence == "low":
        return _nothing(MODEL_LOW_CONFIDENCE, **model)
    destination = _destination(evidence, country_code_fix_enabled=country_code_fix_enabled)
    if destination is None:
        return _nothing(DESTINATION_WITHOUT_PERMISSION, **model)
    if decision == "same_person" and confidence == "high":
        # El rasgo fuerte que cita tiene que ser verdad en la evidencia.
        strong = (
            ("phone_canonical_equal" in codes and evidence.flag("telefono_canonico_igual"))
            or ("email_exact_equal" in codes and evidence.flag("mail_igual"))
            or (
                "phone_national_equal_country_differs" in codes
                and destination == "intent_national_digits"
            )
        )
        if not strong:
            return _nothing(MODEL_WITHOUT_STRONG_TRAIT, **model)
        return IdentityLinkVerdict(branch="ata", destination_source=destination, **model)
    # same_person + medium, o unsure.
    if ask_enabled:
        return IdentityLinkVerdict(branch="pregunta", destination_source=destination, **model)
    return _nothing(WOULD_HAVE_ASKED, **model)


class IdentityLinkClient:
    """El modelo del pre-resolutor (la misma conexion), con el prompt identity-link-v1."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        prompt_version: str = PROMPT_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("identity_link_api_key_required")
        if not isinstance(model_name, str) or _SAFE_MODEL_NAME.fullmatch(model_name) is None:
            raise ValueError("invalid_identity_link_model")
        if prompt_version not in SYSTEM_PROMPTS:
            raise ValueError("unsupported_identity_link_prompt")
        self._api_key = api_key
        self.model_name = model_name
        self.prompt_version = prompt_version
        self._transport = transport

    async def propose(self, evidence: IdentityLinkEvidence, *, case_id: str) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=_TIMEOUT, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Idempotency-Key": f"identity-link:{case_id}",
                    },
                    json={
                        "model": self.model_name,
                        "stream": False,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPTS[self.prompt_version]},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    evidence.to_model_context(),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            },
                        ],
                    },
                )
                if response.status_code != 200:
                    raise IdentityLinkProviderError("identity_link_provider_http_error")
                body: Any = response.json()
                content = body["choices"][0]["message"]["content"]
        except IdentityLinkProviderError:
            raise
        except httpx.HTTPError as exc:
            raise IdentityLinkProviderError("identity_link_provider_transport_error") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IdentityLinkProviderError("identity_link_provider_invalid_response") from exc
        proposal = parse_preresolution_proposal(content)
        if proposal is None:
            # Un texto que no es JSON: no es una falla del proveedor, es una
            # respuesta invalida (la rama «nada», sin reintento).
            return {"_invalid": True}
        return proposal


@dataclass(frozen=True)
class IdentityLinkClaim:
    webhook_event_id: str
    claim_token: str
    lease_generation: int
    attempt_count: int
    evidence: Mapping[str, object]
    evidence_sha256: str


class IdentityLinkModel(Protocol):
    model_name: str
    prompt_version: str

    async def propose(
        self, evidence: IdentityLinkEvidence, *, case_id: str
    ) -> dict[str, object]: ...


class IdentityLinkStore(Protocol):
    async def claim_identity_link_case(
        self, *, tenant_ref: str, funnel_ref: str, worker_id: str
    ) -> IdentityLinkClaim | None: ...

    async def complete_identity_link_decision(
        self, *, claim: IdentityLinkClaim, mode: str, verdict: IdentityLinkVerdict
    ) -> dict[str, object]: ...

    async def release_identity_link_case(
        self, *, claim: IdentityLinkClaim, failure_code: str
    ) -> None: ...

    async def requeue_identity_linked_events(self, *, tenant_ref: str, funnel_ref: str) -> int: ...


class IdentityLinkWorker:
    """Decide cada conflicto de carrito de la cola de la base, de a uno."""

    def __init__(
        self,
        *,
        store: IdentityLinkStore,
        model: IdentityLinkModel | None,
        tenant_ref: str,
        funnel_ref: str,
        worker_id: str,
        mode: str,
        rules_first: bool = True,
        country_code_fix_enabled: bool = False,
        ask_enabled: bool = False,
        poll_interval_seconds: float = 15.0,
        extra_round: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        if mode not in {"sombra", "vivo"}:
            raise ValueError("invalid_identity_link_mode")
        if ask_enabled and mode != "vivo":
            raise ValueError("identity_link_ask_requires_vivo")
        for value in (tenant_ref, funnel_ref, worker_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("invalid_identity_link_scope")
        if poll_interval_seconds <= 0:
            raise ValueError("invalid_identity_link_poll_interval")
        self._store = store
        self._model = model
        self._tenant_ref = tenant_ref
        self._funnel_ref = funnel_ref
        self._worker_id = worker_id
        self.mode = mode
        self._rules_first = rules_first
        self._country_fix = country_code_fix_enabled
        self._ask_enabled = ask_enabled
        self._poll_interval_seconds = poll_interval_seconds
        self._extra_round = extra_round
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._healthy = True

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="identity-link-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        await task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
                self._healthy = True
            except Exception as exc:
                self._healthy = False
                logger.warning("identity_link_worker_failed error_type=%s", type(exc).__name__)
                processed = 0
            if processed:
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass

    async def decide(self, claim: IdentityLinkClaim) -> IdentityLinkVerdict | None:
        """La rama de un caso. None: el modelo no contesto y se reintenta."""
        try:
            evidence = IdentityLinkEvidence.from_snapshot(claim.evidence)
        except ValueError:
            return _nothing(EVIDENCE_INVALID)
        verdict = apply_rules(
            evidence,
            rules_first=self._rules_first,
            country_code_fix_enabled=self._country_fix,
        )
        if verdict is not None:
            return verdict
        if self._model is None:
            return _nothing(MODEL_UNAVAILABLE)
        try:
            proposal = await self._model.propose(evidence, case_id=claim.webhook_event_id)
        except IdentityLinkProviderError:
            if claim.attempt_count >= MAX_MODEL_ATTEMPTS:
                return _nothing(
                    MODEL_UNAVAILABLE,
                    decided_by=f"modelo:{self._model.model_name}",
                    prompt_version=self._model.prompt_version,
                )
            return None
        return verdict_from_model(
            proposal,
            evidence,
            model_name=self._model.model_name,
            ask_enabled=self._ask_enabled,
            country_code_fix_enabled=self._country_fix,
            prompt_version=self._model.prompt_version,
        )

    async def run_once(self) -> int:
        try:
            await self._store.requeue_identity_linked_events(
                tenant_ref=self._tenant_ref, funnel_ref=self._funnel_ref
            )
        except Exception as exc:
            logger.warning("identity_link_requeue_failed error_type=%s", type(exc).__name__)
        if self._extra_round is not None:
            try:
                await self._extra_round()
            except Exception as exc:
                logger.warning("identity_link_extra_round_failed error_type=%s", type(exc).__name__)
        claim = await self._store.claim_identity_link_case(
            tenant_ref=self._tenant_ref, funnel_ref=self._funnel_ref, worker_id=self._worker_id
        )
        if claim is None:
            return 0
        verdict = await self.decide(claim)
        if verdict is None:
            await self._store.release_identity_link_case(
                claim=claim, failure_code="identity_link_model_retry"
            )
            return 0
        try:
            result = await self._store.complete_identity_link_decision(
                claim=claim, mode=self.mode, verdict=verdict
            )
        except Exception as exc:
            # La base no acepto la decision (re-verifica la regla, el rasgo
            # fuerte y el destino). Queda «nada» con su motivo, para no volver
            # a tomar el mismo caso en cada vuelta. Si tampoco eso entra (la
            # base no responde), se suelta y el error sube.
            logger.warning(
                "identity_link_decision_refused event_id=%s branch=%s error=%s",
                claim.webhook_event_id,
                verdict.branch,
                type(exc).__name__,
            )
            if verdict.branch == "nada":
                await self._store.release_identity_link_case(
                    claim=claim, failure_code="identity_link_complete_failed"
                )
                raise
            verdict = _nothing(DECISION_REFUSED)
            result = await self._store.complete_identity_link_decision(
                claim=claim, mode=self.mode, verdict=verdict
            )
        logger.info(
            "identity_link_decided event_id=%s mode=%s branch=%s decided_by=%s guard=%s effect=%s",
            claim.webhook_event_id,
            self.mode,
            result.get("branch"),
            verdict.decided_by,
            result.get("guard_reason"),
            result.get("effect"),
        )
        return 1


# ---------------------------------------------------------------------------
# Los avisos al canal de alertas de la aliada (decision con default de E-536:
# un mensaje por decision que ata o pregunta, y uno por envio que sale o falla).
# ---------------------------------------------------------------------------

DECISION_EVENT_CODE = "IDL-001"
SEND_EVENT_CODE = "IDL-002"
_NOTICE_NAMESPACE = uuid.UUID("6f1d3c52-6a0e-4c43-9f0b-2b1e5e7c0d36")


@dataclass(frozen=True)
class IdentityLinkNotice:
    notice_key: str
    kind: str
    branch: str
    decided_by: str
    outcome: str | None
    conversation_id: int | None
    occurred_at: datetime


def notice_command(notice: IdentityLinkNotice) -> Any:
    """El aviso sin telefonos ni mails: la rama, la regla y el numero de conversacion."""
    from slack_correlation.catalog import NotificationCommand

    reason = re.sub(r"[^a-z0-9_.:-]", "_", f"{notice.branch}:{notice.decided_by}".lower())[:80]
    component = (
        f"chatwoot.conversation.{notice.conversation_id}"
        if notice.conversation_id is not None
        else "identity_link.sin_conversacion"
    )
    state = notice.outcome if notice.kind == "envio" else "vivo"
    return NotificationCommand(
        event_id=str(uuid.uuid5(_NOTICE_NAMESPACE, notice.notice_key)),
        event_code=DECISION_EVENT_CODE if notice.kind == "decision" else SEND_EVENT_CODE,
        dedupe_key=hashlib.sha256(notice.notice_key.encode("ascii")).hexdigest(),
        occurred_at=notice.occurred_at.astimezone(UTC),
        reason_code=reason,
        component=component,
        state=state,
    )


class IdentityLinkNoticeStore(Protocol):
    async def read_identity_link_notices(
        self, *, tenant_ref: str, funnel_ref: str, limit: int
    ) -> list[IdentityLinkNotice]: ...

    async def mark_identity_link_notice(
        self, *, tenant_ref: str, funnel_ref: str, notice_key: str
    ) -> bool: ...


class NoticeProducer(Protocol):
    async def admit(self, command: Any) -> object: ...


class IdentityLinkNotifier:
    """Avisa al instante en el canal de la aliada (el productor del conector de Slack)."""

    def __init__(
        self,
        *,
        store: IdentityLinkNoticeStore,
        producer: NoticeProducer | None,
        tenant_ref: str,
        funnel_ref: str,
        poll_interval_seconds: float = 15.0,
    ) -> None:
        self._store = store
        self._producer = producer
        self._tenant_ref = tenant_ref
        self._funnel_ref = funnel_ref
        self._poll_interval = poll_interval_seconds
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.alerts = "slack" if producer is not None else "sin_slack"

    async def run_once(self) -> int:
        if self._producer is None:
            return 0
        notices = await self._store.read_identity_link_notices(
            tenant_ref=self._tenant_ref, funnel_ref=self._funnel_ref, limit=20
        )
        told = 0
        for notice in notices:
            try:
                await self._producer.admit(notice_command(notice))
            except Exception as exc:
                if type(exc).__name__ != "ConnectorSemanticConflict":
                    logger.warning(
                        "identity_link_notice_failed key=%s error=%s",
                        notice.notice_key,
                        type(exc).__name__,
                    )
                    continue
            await self._store.mark_identity_link_notice(
                tenant_ref=self._tenant_ref, funnel_ref=self._funnel_ref, notice_key=notice.notice_key
            )
            told += 1
            logger.info(
                "identity_link_notice_sent kind=%s branch=%s outcome=%s",
                notice.kind,
                notice.branch,
                notice.outcome,
            )
        return told

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("identity_link_notifier_unexpected_error")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Bloque B: la pregunta («¿fuiste tú?»), con su plantilla y sus dos botones.
# ---------------------------------------------------------------------------

# Los botones de respuesta rapida de la plantilla (texto aprobado por Dan el
# 13/11). «No más mensajes» es exactamente el boton de baja del producto
# (template_request.OPT_OUT_BUTTON): la entrada de Chatwoot lo detecta como baja
# (opt_out.detect_explicit_opt_out) y la registra por el camino de siempre.
CONFIRM_BUTTON = "Sí, fui yo"
OPT_OUT_BUTTON = "No más mensajes"
QUESTION_DEFAULT_WINDOW = (time(9, 0), time(21, 0))


def _normalized_reply(value: object) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split()).strip().lower()
    return collapsed.replace("í", "i").replace("¡", "").replace("!", "").replace(".", "")


def answer_for_reply(content: object) -> str | None:
    """«si» con el boton de confirmacion, «baja» con el de baja; None con otro texto."""
    normalized = _normalized_reply(content)
    if normalized == _normalized_reply(CONFIRM_BUTTON):
        return "si"
    if normalized == _normalized_reply(OPT_OUT_BUTTON):
        return "baja"
    return None


@dataclass(frozen=True)
class IdentityQuestionTemplate:
    """[plantillas].identidad del manifiesto: el nombre en Meta y sus variables.

    El texto lo define Dan y lo aprueba Meta; aca solo se nombra. Las variables,
    en el orden en que la plantilla aprobada las numera: nombre (el saludo del
    formulario), producto y marca (``instancia.marca``, el nombre con que se
    presenta la aliada). Por defecto, el del texto aprobado el 13/11: «Hola {{1}},
    te escribe el equipo de {{3}}. Vimos que empezaste tu compra de {{2}}. …».
    """

    name: str
    language: str
    category: str = "MARKETING"
    parameters: tuple[str, ...] = ("nombre", "producto", "marca")

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,512}", self.name or ""):
            raise ValueError("invalid_identity_question_template_name")
        if not re.fullmatch(r"[a-z]{2}(_[A-Z]{2})?", self.language or ""):
            raise ValueError("invalid_identity_question_template_language")
        if self.category not in {"MARKETING", "UTILITY"}:
            raise ValueError("invalid_identity_question_template_category")
        if (
            not self.parameters
            or len(set(self.parameters)) != len(self.parameters)
            or any(p not in {"nombre", "producto", "marca"} for p in self.parameters)
        ):
            raise ValueError("invalid_identity_question_template_parameters")

    def params(self, *, greeting_name: str, product_name: str, brand: str) -> dict[str, object]:
        values = {"nombre": greeting_name, "producto": product_name, "marca": brand}
        body = {str(index): values[name] for index, name in enumerate(self.parameters, start=1)}
        return {
            "name": self.name,
            "category": self.category,
            "language": self.language,
            "processed_params": {"body": body},
        }


@dataclass(frozen=True)
class IdentityQuestionClaim:
    decision_id: str
    claim_token: str
    lease_generation: int
    phone: str
    full_name: str | None
    product_name: str | None


class IdentityQuestionStore(Protocol):
    async def expire_identity_link_questions(self, *, tenant_ref: str, funnel_ref: str) -> int: ...

    async def claim_identity_link_question(
        self, *, tenant_ref: str, funnel_ref: str, worker_id: str
    ) -> IdentityQuestionClaim | None: ...

    async def start_identity_link_question(self, *, claim: IdentityQuestionClaim) -> str: ...

    async def finish_identity_link_question(
        self,
        *,
        claim: IdentityQuestionClaim,
        outcome: str,
        conversation_id: int | None,
        message_id: int | None,
        failure_code: str | None,
    ) -> None: ...


class QuestionSender(Protocol):
    async def send_first_touch(self, **kwargs: Any) -> Any: ...


@dataclass
class IdentityQuestionRound:
    """Una vuelta de la pregunta: vence lo viejo y manda, de a una, la que toca.

    La puerta antes de enviar es la base (start_identity_link_question_v1): los
    frenos en vivo (permiso, baja, compra, carrito viejo). Fuera de la franja de
    la aliada no se manda: la pregunta espera (vence a las 24 h del carrito). Un
    envio de resultado incierto no se reintenta nunca.
    """

    store: IdentityQuestionStore
    sender: QuestionSender
    template: IdentityQuestionTemplate
    tenant_ref: str
    funnel_ref: str
    worker_id: str
    # instancia.marca: el nombre con que se presenta la aliada ({{3}} del texto aprobado).
    brand: str = ""
    time_zone: str = "America/Mexico_City"
    window: tuple[time, time] = QUESTION_DEFAULT_WINDOW
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    greeting: Callable[[str | None], str | None] | None = None

    def in_window(self) -> bool:
        local = self.clock().astimezone(ZoneInfo(self.time_zone)).time()
        start, end = self.window
        return start <= local < end

    def _greeting(self, full_name: str | None) -> str | None:
        if self.greeting is not None:
            return self.greeting(full_name)
        from bridge.lead_first_name import template_greeting_name_is_safe
        from bridge.reactivation import reactivation_first_name

        name = reactivation_first_name(full_name) if full_name else None
        return name if name is not None and template_greeting_name_is_safe(name) else None

    async def __call__(self) -> int:
        await self.store.expire_identity_link_questions(
            tenant_ref=self.tenant_ref, funnel_ref=self.funnel_ref
        )
        if not self.in_window():
            return 0
        claim = await self.store.claim_identity_link_question(
            tenant_ref=self.tenant_ref, funnel_ref=self.funnel_ref, worker_id=self.worker_id
        )
        if claim is None:
            return 0
        greeting = self._greeting(claim.full_name)
        product = (claim.product_name or "").strip()
        brand = " ".join(self.brand.split())
        if greeting is None or not product or ("marca" in self.template.parameters and not brand):
            await self.store.finish_identity_link_question(
                claim=claim,
                outcome="failed",
                conversation_id=None,
                message_id=None,
                failure_code="template_parameters_missing",
            )
            return 1
        started = await self.store.start_identity_link_question(claim=claim)
        if started != "ok":
            logger.info("identity_link_question_not_started reason=%s", started)
            return 1
        try:
            result = await self.sender.send_first_touch(
                phone=claim.phone,
                buyer_name=claim.full_name,
                buyer_email=None,
                product_name=product,
                content="",
                delivery_id=f"identity-link:{claim.decision_id}",
                greeting_name=greeting,
                template_params=self.template.params(
                    greeting_name=greeting, product_name=product, brand=brand
                ),
            )
        except Exception as exc:
            # El pedido salio o no: incierto. No se reintenta.
            await self.store.finish_identity_link_question(
                claim=claim,
                outcome="unknown",
                conversation_id=None,
                message_id=None,
                failure_code=f"sender_{type(exc).__name__.lower()}"[:80],
            )
            return 1
        status = getattr(result, "status", None)
        await self.store.finish_identity_link_question(
            claim=claim,
            outcome="sent" if status == "sent" else "failed",
            conversation_id=getattr(result, "conversation_id", None),
            message_id=getattr(result, "message_id", None),
            failure_code=None if status == "sent" else (getattr(result, "reason", None) or "send_failed")[:80],
        )
        logger.info(
            "identity_link_question_finished status=%s conversation=%s",
            status,
            getattr(result, "conversation_id", None),
        )
        return 1


class IdentityAnswerStore(Protocol):
    async def record_identity_link_answer(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        conversation_id: int,
        answer: str,
        message_id: int | None,
    ) -> dict[str, object]: ...


async def record_reply_if_answer(
    store: IdentityAnswerStore,
    *,
    tenant_ref: str,
    funnel_ref: str,
    conversation_id: int,
    content: object,
    message_id: object,
) -> str | None:
    """La entrada de Chatwoot: si el texto es un boton de la pregunta, lo registra.

    Falla blanda: la conversacion sigue su camino de siempre (el agente con la
    «Sí», la baja de siempre con «No más mensajes»). Devuelve la respuesta
    registrada, o None.
    """
    answer = answer_for_reply(content)
    if answer is None:
        return None
    try:
        result = await store.record_identity_link_answer(
            tenant_ref=tenant_ref,
            funnel_ref=funnel_ref,
            conversation_id=conversation_id,
            answer=answer,
            message_id=message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
        )
    except Exception as exc:
        logger.warning(
            "identity_link_answer_failed conversation=%s error=%s",
            conversation_id,
            type(exc).__name__,
        )
        return None
    if not result.get("recorded"):
        return None
    logger.info(
        "identity_link_answer_recorded conversation=%s answer=%s",
        conversation_id,
        answer,
    )
    return answer

