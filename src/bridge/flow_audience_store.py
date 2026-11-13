"""El adaptador de la base y la fuente de la audiencia del disparo por audiencia (E-484).

Contrato: ``docs/contracts/flow-audience-trigger-v1.md``; la base:
``20261112220000_flow_audience_triggers_v1.sql``.

* ``SupabaseAudienceStore``: las funciones del bridge (solo ``service_role``):
  los flujos de audiencia que corren, la audiencia posible de un flujo, armar la
  lista, frenar, soltar, asentar lo que contesto el motor y leer las ocurrencias.
  Un error de estado vuelve como ``{"ok": false, "reason_code"}``; una entrada
  invalida o algo que no existe (22023, P0002) es ``AudienceStoreRejected``; lo
  demas (la red, un 5xx, una respuesta fuera de forma) es ``AudienceStoreUnavailable``.
* ``BaseAudienceSource``: la audiencia posible sale de la base (formularios con
  permiso, compras, baja, contacto) y, si el flujo pide ``etiquetas`` o
  ``excluye``, las etiquetas salen de Chatwoot (``ChatwootLabelIndex``).

Nada de esto imprime un telefono: los logs llevan ids de flujo y conteos.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID

import httpx

from bridge.flow_audience import AudienceSpec, Candidate, Occurrence
from bridge.flow_audience_sweeper import AudienceFlow, OccurrenceRow, ReleasedMember
from bridge.phones import equivalent_whatsapp_phones

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 8_000_000
CANDIDATES_LIMIT = 20_000
OCCURRENCES_LIMIT = 200
_PHONE = re.compile(r"[1-9][0-9]{7,14}")
_REASON = re.compile(r"[a-z][a-z0-9_.:-]{0,79}")
OPEN_OUTCOMES = frozenset({"arranco", "no_arranca", "sin_flujo"})


class AudienceStoreUnavailable(Exception):
    """La base no contesto o contesto algo fuera del contrato: se reintenta en la pasada siguiente."""


class AudienceStoreRejected(Exception):
    """La base rechazo la entrada (22023) o no encontro lo pedido (P0002), con su motivo."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid(value: object) -> str | None:
    try:
        return str(UUID(str(value))) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class CandidateRead:
    """La audiencia posible de un flujo y lo que la base no pudo atar a una persona."""

    candidates: tuple[Candidate, ...]
    without_contact: int
    ambiguous: int


class SupabaseAudienceStore:
    """Las funciones del bridge, con la clave de ``service_role``. Ver el docstring del modulo."""

    def __init__(self, *, base_url: str, service_role_key: str, instance: Mapping[str, str],
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._instance = dict(instance)
        self._transport = transport
        self._timeout = timeout_seconds

    async def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=self._transport,
                                         timeout=self._timeout) as client:
                response = await client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        except httpx.HTTPError as error:
            raise AudienceStoreUnavailable(f"{function}_unavailable") from error
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AudienceStoreUnavailable(f"{function}_too_large")
        try:
            body = response.json()
        except ValueError as error:
            raise AudienceStoreUnavailable(f"{function}_not_json") from error
        if response.status_code == 200:
            return body
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if code in ("22023", "P0002") and isinstance(message, str) and _REASON.fullmatch(message):
            raise AudienceStoreRejected(message)
        raise AudienceStoreUnavailable(f"{function}_http_{response.status_code}")

    async def _object(self, function: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = await self._rpc(function, payload)
        if not isinstance(body, dict) or not isinstance(body.get("ok", True), bool):
            raise AudienceStoreUnavailable(f"{function}_invalid_shape")
        return body

    async def read_audience_flows(self) -> Sequence[AudienceFlow]:
        body = await self._rpc("read_flow_audience_flows_v1", {"p_instance": self._instance})
        if not isinstance(body, list):
            raise AudienceStoreUnavailable("read_flow_audience_flows_v1_invalid_shape")
        flows = []
        for row in body:
            flow_id = _uuid(row.get("flow_id")) if isinstance(row, dict) else None
            if (flow_id is None or not isinstance(row.get("version"), int) or not isinstance(row.get("flow_key"), str)
                    or not isinstance(row.get("definition"), dict) or not isinstance(row.get("product_ref"), str)):
                raise AudienceStoreUnavailable("read_flow_audience_flows_v1_invalid_row")
            flows.append(AudienceFlow(flow_id=flow_id, version=row["version"], flow_key=row["flow_key"],
                                      definition=row["definition"], product_ref=row["product_ref"],
                                      state=str(row.get("state") or "")))
        return flows

    async def read_candidates(self, flow: AudienceFlow, *, limit: int = CANDIDATES_LIMIT) -> CandidateRead:
        body = await self._object("read_flow_audience_candidates_v1", {
            "p_instance": self._instance, "p_flow_id": flow.flow_id, "p_version": flow.version, "p_limit": limit})
        rows = body.get("candidates")
        if not isinstance(rows, list):
            raise AudienceStoreUnavailable("read_flow_audience_candidates_v1_invalid_shape")
        out = []
        for row in rows:
            contact = _uuid(row.get("contact_id")) if isinstance(row, dict) else None
            phone = row.get("phone") if isinstance(row, dict) else None
            products = row.get("customer_products") if isinstance(row, dict) else None
            if (contact is None or not isinstance(phone, str) or not _PHONE.fullmatch(phone)
                    or not isinstance(products, list) or not isinstance(row.get("opted_out"), bool)):
                raise AudienceStoreUnavailable("read_flow_audience_candidates_v1_invalid_row")
            form_at = row.get("form_at")
            try:
                when = datetime.fromisoformat(form_at) if isinstance(form_at, str) else None
            except ValueError as error:
                raise AudienceStoreUnavailable("read_flow_audience_candidates_v1_invalid_row") from error
            out.append(Candidate(contact_id=contact, phone=phone, labels=frozenset(), opted_out=row["opted_out"],
                                 form_at=when, customer_products=frozenset(str(p) for p in products)))
        return CandidateRead(tuple(out), int(body.get("sin_contacto") or 0), int(body.get("ambiguas") or 0))

    async def materialize(self, flow: AudienceFlow, occurrence: Occurrence, members: Sequence[Candidate],
                          fingerprint: str, *, materialized_by: str = "barredor") -> Mapping[str, Any]:
        return await self._object("materialize_flow_audience_occurrence_v1", {
            "p_instance": self._instance, "p_flow_id": flow.flow_id, "p_version": flow.version,
            "p_occurrence": {"key": occurrence.key, "scheduled_at": occurrence.scheduled_at.isoformat(),
                             "fingerprint": fingerprint, "materialized_by": materialized_by},
            "p_members": [{"contact_id": m.contact_id, "phone": m.phone} for m in members]})

    async def read_occurrences(self) -> Sequence[OccurrenceRow]:
        body = await self._rpc("read_flow_audience_occurrences_v1",
                               {"p_instance": self._instance, "p_limit": OCCURRENCES_LIMIT})
        if not isinstance(body, list):
            raise AudienceStoreUnavailable("read_flow_audience_occurrences_v1_invalid_shape")
        rows = []
        for row in body:
            occurrence = _uuid(row.get("occurrence_id")) if isinstance(row, dict) else None
            flow = _uuid(row.get("flow_id")) if isinstance(row, dict) else None
            if occurrence is None or flow is None or not isinstance(row.get("state"), str):
                raise AudienceStoreUnavailable("read_flow_audience_occurrences_v1_invalid_row")
            rows.append(OccurrenceRow(occurrence_id=occurrence, flow_id=flow, state=row["state"],
                                      form=str(row.get("form") or ""), flow_version=int(row.get("flow_version") or 0)))
        return rows

    async def brake(self, occurrence_id: str, reason: str) -> Mapping[str, Any]:
        return await self._object("brake_flow_audience_occurrence_v1",
                                  {"p_occurrence_id": occurrence_id, "p_reason": reason})

    async def release(self, occurrence_id: str, *, now: datetime, limit: int) -> Mapping[str, Any]:
        body = await self._object("release_flow_audience_members_v1",
                                  {"p_occurrence_id": occurrence_id, "p_now": now.isoformat(), "p_limit": limit})
        if body.get("ok") is not True:
            return body
        released = []
        for row in body.get("released") or ():
            contact = _uuid(row.get("contact_id")) if isinstance(row, dict) else None
            phone = row.get("phone") if isinstance(row, dict) else None
            if contact is None or not isinstance(phone, str) or not _PHONE.fullmatch(phone):
                raise AudienceStoreUnavailable("release_flow_audience_members_v1_invalid_row")
            released.append(ReleasedMember(contact_id=contact, phone=phone, again=row.get("again") is True))
        return {**body, "released": released}

    async def settle(self, occurrence_id: str, contact_id: str, outcome: str) -> Mapping[str, Any]:
        if outcome not in OPEN_OUTCOMES:
            raise ValueError(f"open outcome not supported: {outcome}")
        return await self._object("settle_flow_audience_member_v1",
                                  {"p_occurrence_id": occurrence_id, "p_contact_id": contact_id, "p_outcome": outcome})


# ---------------------------------------------------------------------------
# La fuente.
# ---------------------------------------------------------------------------

class LabelIndex(Protocol):
    async def labels_by_phone(self, *, active_days: int) -> Mapping[str, frozenset[str]]: ...


class AudienceLabelsUnavailable(Exception):
    """Las etiquetas de Chatwoot no se pudieron leer enteras: la ocurrencia no se arma (falla cerrado)."""


class ChatwootLabelIndex:
    """Las etiquetas de las conversaciones de la bandeja con actividad en la ventana, por telefono.

    Usa el listado que ya usa el aviso de derivaciones
    (``list_recent_conversations_with_messages(include_messages=False)``): cada item
    trae ``labels`` y ``meta.sender.phone_number``. Un listado incompleto (mas de
    20 paginas por estado) o un item con etiquetas y sin telefono es
    ``AudienceLabelsUnavailable``: una audiencia por etiqueta no sale a medias.
    """

    STATUSES = ("open", "pending", "snoozed", "resolved")
    MAX_PAGES = 20

    def __init__(self, *, chatwoot: Any, inbox_id: int, clock: Any = None) -> None:
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def labels_by_phone(self, *, active_days: int) -> Mapping[str, frozenset[str]]:
        since = int((self._clock() - timedelta(days=active_days)).timestamp())
        try:
            items = await self._chatwoot.list_recent_conversations_with_messages(
                expected_inbox_id=self._inbox_id, statuses=self.STATUSES, active_since_epoch=since,
                max_pages=self.MAX_PAGES, include_messages=False)
        except Exception as error:  # noqa: BLE001 - cualquier falla de la lectura frena la audiencia
            raise AudienceLabelsUnavailable(type(error).__name__) from error
        out: dict[str, set[str]] = {}
        for item in items:
            conversation = item.get("conversation") if isinstance(item, Mapping) else None
            if not isinstance(conversation, Mapping):
                raise AudienceLabelsUnavailable("item")
            labels = conversation.get("labels") or []
            if not isinstance(labels, list):
                raise AudienceLabelsUnavailable("labels")
            sender = (conversation.get("meta") or {}).get("sender") if isinstance(conversation.get("meta"), Mapping) \
                else None
            phone_raw = sender.get("phone_number") if isinstance(sender, Mapping) else None
            digits = re.sub(r"[^0-9]", "", phone_raw) if isinstance(phone_raw, str) else ""
            if not _PHONE.fullmatch(digits):
                if labels:
                    raise AudienceLabelsUnavailable("sin_telefono")
                continue
            for variant in equivalent_whatsapp_phones(digits) or (digits,):
                out.setdefault(variant, set()).update(str(label) for label in labels)
        return {phone: frozenset(labels) for phone, labels in out.items()}


# Cuantos dias de conversaciones se miran para las etiquetas de una clienta (para
# una prospecta, la ventana del formulario).
CUSTOMER_LABEL_DAYS = 90


class BaseAudienceSource:
    """La audiencia posible: la base, y Chatwoot solo si el flujo pide etiquetas."""

    def __init__(self, *, store: SupabaseAudienceStore, labels: LabelIndex | None) -> None:
        self._store = store
        self._labels = labels

    async def candidates(self, flow: AudienceFlow, spec: AudienceSpec) -> Iterable[Candidate]:
        read = await self._store.read_candidates(flow)
        if read.without_contact or read.ambiguous:
            logger.info("flow_audience_candidates flow_key=%s without_contact=%s ambiguous=%s",
                        flow.flow_key, read.without_contact, read.ambiguous)
        if not spec.labels and not spec.excluded:
            return read.candidates
        if self._labels is None:
            raise AudienceLabelsUnavailable("sin_chatwoot")
        days = spec.form_window_days if spec.status == "prospecta" else CUSTOMER_LABEL_DAYS
        index = await self._labels.labels_by_phone(active_days=days)
        out = []
        for candidate in read.candidates:
            labels: set[str] = set()
            for variant in equivalent_whatsapp_phones(candidate.phone) or (candidate.phone,):
                labels |= index.get(variant, frozenset())
            out.append(Candidate(contact_id=candidate.contact_id, phone=candidate.phone, labels=frozenset(labels),
                                 opted_out=candidate.opted_out, form_at=candidate.form_at,
                                 customer_products=candidate.customer_products))
        return out
