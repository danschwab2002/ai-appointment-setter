"""Los conectores del permiso de WhatsApp: el aviso en el checkout y en la landing (E-628, M2 de E-624).

Contrato: docs/contracts/permission-registry-v1.md (§8). El nucleo solo pregunta
si hay un permiso registrado (``_registered_permission_reason``); de donde salio
lo resuelve un conector. Estos dos convierten un evento de Hotmart en un permiso
cuando la instancia declara un aviso que la persona vio en el paso:

- ``hotmart_checkout``: el aviso esta en la pagina de pago de Hotmart, arriba del
  telefono o del boton. El carrito prueba que la persona llego al checkout y
  cargo sus datos debajo del aviso.
- ``landing``: el aviso esta pegado al boton de compra de una landing cuya oferta
  solo se llega desde ahi (``oferta_exclusiva``, una afirmacion firmada en el
  manifiesto, como un formulario de ``[adaptadores.ghl]``). Con ``requiere_src``,
  un pago rechazado tiene que traer ese ``origin.src`` (o ``origin.sck``); el
  carrito no trae ``src`` y no se le puede exigir.

Los avisos salen de ``[[permisos.avisos]]`` del manifiesto y se cargan en la base
con ``register_permission_notice_v1`` (el sha256 del texto en UTF-8). Por cada
carrito o pago rechazado con telefono que va a la admision nativa, el conector
escribe a lo sumo un permiso con ``record_permission_grant_v1``, **antes** de la
admision, con el id del evento como evidencia. Un ``rejected`` de la base no es
un error: se anota en el log y el evento sigue como hoy. Una falla tampoco frena
el evento: sin permiso, la admision termina en ``no_entra:permiso`` como antes.

Todo esto solo con ``PERMISSION_NOTICE_CONNECTOR_ENABLED=true``. Apagado (el
default) no se carga ningun aviso, no se escribe ningun permiso y ``/ready`` no
cambia. Sin datos personales en el log: el evento, el aviso, el resultado y el
motivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Mapping, Protocol

from bridge.hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_CANCELED

logger = logging.getLogger(__name__)

PERMISSION_NOTICE_CONNECTOR_ENV = "PERMISSION_NOTICE_CONNECTOR_ENABLED"

CONNECTOR_HOTMART_CHECKOUT = "hotmart_checkout"
CONNECTOR_LANDING = "landing"
# El orden es la preferencia: con dos avisos que cubren el mismo evento, el del
# checkout gana (la persona escribio el telefono debajo del aviso).
CONNECTORS = (CONNECTOR_HOTMART_CHECKOUT, CONNECTOR_LANDING)
CONNECTOR_VERSION = "v1"
EVIDENCE_KIND = "aviso_en_el_paso"
USE_RECOVERY = "recuperacion"
# v1: solo recuperacion. La difusion con permisos de aviso es una decision aparte.
USES = (USE_RECOVERY,)
CHECKBOXES = ("ninguna", "opcional_sin_premarcar", "obligatoria")
# Los eventos que el nucleo mira con el registro (contrato §5): el carrito y el
# pago rechazado. La ficha, la compra y la intencion no.
EVENT_TYPES = (EVENT_CART_ABANDONMENT, EVENT_PURCHASE_CANCELED)
# La ventana para entrar, en horas: el mismo tope que max_lookback (30 dias).
MAX_WINDOW_HOURS = 720

# Lo que el conector deja en el log cuando no escribe nada.
SKIP_WITHOUT_PHONE = "sin_telefono"
SKIP_WITHOUT_OFFER = "sin_oferta"
SKIP_EVENT_TYPE = "evento_sin_permiso"
SKIP_WITHOUT_NOTICE = "sin_aviso"
SKIP_SRC = "src_distinto"


def notice_text_sha256(text: str) -> str:
    """El sha256 del texto del aviso en UTF-8, como lo verifica la base."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class PermissionNotice:
    """Un aviso declarado (``[[permisos.avisos]]``), ya validado por el manifiesto.

    ``product_ref`` es el ``funnel_ref`` del producto (el de la admision nativa);
    ``offer_refs`` vacio = todo el producto (solo ``hotmart_checkout``).
    ``window`` es la ventana para entrar de cada permiso que respalda: el
    ``max_lookback`` del scope de la oferta, declarado en el manifiesto porque el
    bridge no lee esa tabla.
    """

    notice_ref: str
    connector: str
    product_ref: str
    offer_refs: tuple[str, ...]
    uses: tuple[str, ...]
    surface_url: str
    copy_version: str
    text: str
    checkbox: str
    measured_at: datetime
    measured_by: str
    valid_from: datetime
    valid_until: datetime | None
    window: timedelta
    requires_src: str | None = None
    exclusive_offer: bool = False

    @property
    def text_sha256(self) -> str:
        return notice_text_sha256(self.text)

    def to_rpc(self, tenant_ref: str) -> dict[str, Any]:
        """Las claves de ``register_permission_notice_v1`` (contrato §3)."""
        body: dict[str, Any] = {
            "tenant_ref": tenant_ref,
            "notice_ref": self.notice_ref,
            "connector": self.connector,
            "product_ref": self.product_ref,
            "offer_refs": list(self.offer_refs),
            "uses": list(self.uses),
            "surface_url": self.surface_url,
            "copy_version": self.copy_version,
            "notice_text": self.text,
            "notice_text_sha256": self.text_sha256,
            "checkbox": self.checkbox,
            "measured_at": _iso(self.measured_at),
            "measured_by": self.measured_by,
            "valid_from": _iso(self.valid_from),
        }
        if self.valid_until is not None:
            body["valid_until"] = _iso(self.valid_until)
        if self.requires_src is not None:
            body["requires_src"] = self.requires_src
        return body

    def valid_at(self, moment: datetime) -> bool:
        return self.valid_from <= moment and (
            self.valid_until is None or moment < self.valid_until
        )

    def covers(self, product_ref: str, offer_code: str) -> bool:
        return self.product_ref == product_ref and (
            not self.offer_refs or offer_code in self.offer_refs
        )


def event_origin_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """``data.purchase.origin.src`` y ``.sck`` del evento, los que vengan.

    El carrito no trae ``purchase``: devuelve vacio.
    """
    data = payload.get("data")
    purchase = data.get("purchase") if isinstance(data, Mapping) else None
    origin = purchase.get("origin") if isinstance(purchase, Mapping) else None
    if not isinstance(origin, Mapping):
        return ()
    return tuple(
        value
        for value in (origin.get("src"), origin.get("sck"))
        if isinstance(value, str) and value
    )


@dataclass(frozen=True)
class NoticeChoice:
    notice: PermissionNotice | None
    skip_reason: str | None = None


def choose_notice(
    notices: tuple[PermissionNotice, ...],
    *,
    event_type: str,
    product_ref: str,
    offer_code: str | None,
    at: datetime,
    payload: Mapping[str, Any],
) -> NoticeChoice:
    """El aviso que respalda este evento, o por que ninguno.

    Del conector preferido (``CONNECTORS``) y, dentro de uno, el de
    ``valid_from`` mas nuevo. Un aviso de landing con ``requiere_src`` no
    respalda un pago rechazado sin ese ``src``: se prueba el siguiente.
    """
    if event_type not in EVENT_TYPES:
        return NoticeChoice(None, SKIP_EVENT_TYPE)
    if offer_code is None:
        return NoticeChoice(None, SKIP_WITHOUT_OFFER)
    candidates = [
        notice
        for notice in notices
        if notice.covers(product_ref, offer_code)
        and notice.valid_at(at)
        and USE_RECOVERY in notice.uses
    ]
    if not candidates:
        return NoticeChoice(None, SKIP_WITHOUT_NOTICE)
    origin_refs = event_origin_refs(payload)
    src_rejected = False
    for connector in CONNECTORS:
        for notice in sorted(
            (notice for notice in candidates if notice.connector == connector),
            key=lambda notice: (notice.valid_from, notice.notice_ref),
            reverse=True,
        ):
            if (
                notice.requires_src is not None
                and event_type == EVENT_PURCHASE_CANCELED
                and notice.requires_src not in origin_refs
            ):
                src_rejected = True
                continue
            return NoticeChoice(notice)
    return NoticeChoice(None, SKIP_SRC if src_rejected else SKIP_WITHOUT_NOTICE)


class PermissionRegistryStore(Protocol):
    async def register_permission_notice(self, notice: dict[str, Any]) -> str: ...

    async def close_permission_notice(
        self, *, tenant_ref: str, notice_ref: str, valid_until: str
    ) -> str: ...

    async def record_permission_grant(
        self, grant: dict[str, Any]
    ) -> tuple[str | None, str, str | None]: ...


@dataclass(frozen=True)
class GrantResult:
    """Lo que hizo el conector con un evento. ``outcome`` None = no llamo a la base."""

    outcome: str | None
    reason: str | None = None
    notice_ref: str | None = None
    grant_id: str | None = None


class PermissionNoticeConnectors:
    """Los avisos de la instancia y los dos conectores que escriben permisos.

    Los avisos se cargan en la base una vez (``register_notices``, al arrancar y,
    si fallo, antes del primer evento). Sin carga buena no se escribe ningun
    permiso: la base rechazaria cada uno con ``aviso_desconocido``.
    """

    def __init__(
        self,
        store: PermissionRegistryStore,
        *,
        tenant_ref: str,
        notices: tuple[PermissionNotice, ...],
    ) -> None:
        self._store = store
        self._tenant_ref = tenant_ref
        self._notices = notices
        self._registered = False

    @property
    def notices(self) -> tuple[PermissionNotice, ...]:
        return self._notices

    @property
    def registered(self) -> bool:
        return self._registered

    async def register_notices(self) -> bool:
        """Carga cada aviso en la base (y cierra el que el manifiesto cierra).

        Idempotente: un aviso igual vuelve ``unchanged``. Un ``permission_notice_conflict``
        (otro texto con el mismo ``ref``) o la base caida dejan la carga en falso,
        con el motivo en el log, y se reintenta antes del proximo evento.
        """
        try:
            for notice in self._notices:
                outcome = await self._store.register_permission_notice(
                    notice.to_rpc(self._tenant_ref)
                )
                logger.info(
                    "permission_notice_registered notice=%s connector=%s outcome=%s",
                    notice.notice_ref,
                    notice.connector,
                    outcome,
                )
                if notice.valid_until is not None:
                    closed = await self._store.close_permission_notice(
                        tenant_ref=self._tenant_ref,
                        notice_ref=notice.notice_ref,
                        valid_until=_iso(notice.valid_until),
                    )
                    logger.info(
                        "permission_notice_closed notice=%s outcome=%s",
                        notice.notice_ref,
                        closed,
                    )
        except Exception as exc:  # noqa: BLE001 - la carga no tumba el arranque
            logger.warning(
                "permission_notice_register_failed error_type=%s error=%s",
                type(exc).__name__,
                str(exc)[:200],
            )
            self._registered = False
            return False
        self._registered = True
        return True

    def readiness(self, now: datetime) -> str:
        """``<n>-activos`` (los avisos vigentes ahora), o ``sin_cargar`` si la carga no salio."""
        if not self._registered:
            return "sin_cargar"
        active = sum(1 for notice in self._notices if notice.valid_at(now))
        return f"{active}-activos"

    async def record_for_event(
        self,
        *,
        webhook_event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        product_ref: str,
        offer_code: str | None,
        phone: str | None,
        email: str | None,
        contact_id: str | None,
        creation_date_ms: int,
    ) -> GrantResult:
        """Escribe a lo sumo un permiso para este evento. Nunca levanta."""
        if phone is None:
            # Sin telefono no hay a quien escribirle: no se registra nada.
            return self._skip(webhook_event_id, SKIP_WITHOUT_PHONE)
        granted_at = datetime.fromtimestamp(creation_date_ms / 1000, tz=timezone.utc)
        choice = choose_notice(
            self._notices,
            event_type=event_type,
            product_ref=product_ref,
            offer_code=offer_code,
            at=granted_at,
            payload=payload,
        )
        if choice.notice is None:
            return self._skip(webhook_event_id, choice.skip_reason)
        notice = choice.notice
        if not self._registered and not await self.register_notices():
            return GrantResult(None, "avisos_sin_cargar", notice.notice_ref)
        grant = {
            "tenant_ref": self._tenant_ref,
            "phone": phone,
            "contact_id": contact_id,
            "normalized_email": email,
            "product_ref": product_ref,
            "offer_ref": offer_code,
            "uses": [USE_RECOVERY],
            "connector": notice.connector,
            "connector_version": CONNECTOR_VERSION,
            "evidence_kind": EVIDENCE_KIND,
            "evidence_ref": webhook_event_id,
            "notice_ref": notice.notice_ref,
            "copy_version": notice.copy_version,
            "granted_at": _iso(granted_at),
            "expires_at": _iso(granted_at + notice.window),
        }
        try:
            grant_id, outcome, reason = await self._store.record_permission_grant(grant)
        except Exception as exc:  # noqa: BLE001 - el evento sigue como hoy
            logger.warning(
                "permission_grant_failed event_id=%s notice=%s error_type=%s error=%s",
                webhook_event_id,
                notice.notice_ref,
                type(exc).__name__,
                str(exc)[:200],
            )
            return GrantResult("failed", type(exc).__name__, notice.notice_ref)
        if outcome == "rejected":
            # Lo que el aviso no cubre segun la base: el evento sigue como hoy.
            logger.warning(
                "permission_grant_rejected event_id=%s connector=%s notice=%s reason=%s",
                webhook_event_id,
                notice.connector,
                notice.notice_ref,
                reason or "-",
            )
        else:
            logger.info(
                "permission_grant_recorded event_id=%s connector=%s notice=%s outcome=%s",
                webhook_event_id,
                notice.connector,
                notice.notice_ref,
                outcome,
            )
        return GrantResult(outcome, reason, notice.notice_ref, grant_id)

    @staticmethod
    def _skip(webhook_event_id: str, reason: str | None) -> GrantResult:
        logger.info(
            "permission_grant_skipped event_id=%s reason=%s", webhook_event_id, reason
        )
        return GrantResult(None, reason)
