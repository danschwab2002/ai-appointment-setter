"""La pagina de aprobacion de un cambio de flujo (flow-change-cycle-v1 1.9.0, A′ PR 3a y 3b).

El Automation Expert deja en su DM un boton de URL a ``/flow-approval/p/<id>``
(A′ PR 1). Esta pagina pide entrar con «Sign in with Slack» y muestra la
propuesta: la vista previa guardada al validar, el para que como texto no
verificado, a quien llega, lo que esta en curso, la simulacion, las
advertencias, como se vuelve atras y si hay otra propuesta abierta del mismo
flujo. Desde el PR 3b tambien escribe: aprobar y publicar, descartar, pausar y
sumar un telefono a la cohorte, siempre con el ID de Slack de la sesion.

Las rutas, todas bajo ``/flow-approval/`` y solo con ``FLOW_APPROVAL_ENABLED``
(apagado, no existen y dan 404):

* ``GET /flow-approval/``: la lista de flujos de la instancia (con pausar y la
  cohorte) y las propuestas validadas o aprobadas sin vencer.
* ``GET /flow-approval/p/{proposal_id}``: una propuesta y sus botones.
* ``POST /flow-approval/p/{proposal_id}/publish``: aprobar y publicar, como
  ``_publish`` del comando del operador (relee, catalogo, revalida en fase
  ``publish`` con la clase de quien pidio, aprueba con ``slack``, revalida si
  pasaron 8 minutos, publica con la zona). Lo que llega a todos o corta lo que
  esta en curso pide un segundo paso, que decide el bridge.
* ``POST /flow-approval/p/{proposal_id}/discard``: descartar, con un motivo.
* ``POST /flow-approval/pause``: pausar un flujo con la generacion que se vio.
* ``POST /flow-approval/cohort``: sumar un telefono a la cohorte de un flujo
  nativo. El telefono no se guarda, no se loguea y no se muestra de vuelta.
* ``GET /flow-approval/auth/slack/start?p=<proposal_id>``: el ``state`` y el
  ``nonce`` (la base guarda sus sha256, 10 minutos) y la vuelta al OIDC de
  Slack con el scope ``openid``.
* ``GET /flow-approval/auth/slack/callback``: consume el ``state`` una vez,
  cambia el ``code`` por el token y valida el ``id_token`` (issuer, audiencia,
  vencimiento, ``nonce``, workspace y ``sub`` igual al ``user_id``). Recien
  ahi mira la lista de aprobadores; quien no esta no tiene sesion.
* ``POST /flow-approval/logout``: revoca la sesion (CSRF y Origin).

Todo POST exige el ``Origin`` de la pagina y el token de CSRF (un HMAC de la
sesion), y despues de escribir redirige (PRG). Publicar o pausar desde la
pagina deja un aviso en el canal de alertas por el productor del conector de
Slack (OPS-005 y OPS-003); si no sale, lo escrito no se deshace y la pagina lo
dice.

**La sesion** es un secreto de 256 bits en una cookie ``__Host-`` (Secure,
HttpOnly, SameSite=Lax, Path=/); la base guarda su sha256, a lo sumo 8 horas.
Cada pedido la vuelve a validar en la base y mira que el ID siga en la lista
de esa configuracion.

**La firma del ``id_token``.** El token llega del endpoint de tokens de Slack
por HTTPS, en el mismo pedido que cambia el ``code`` con el secreto de la app:
OpenID Connect Core 1.0, 3.1.3.7, punto 6, deja validar el emisor por TLS en
lugar de la firma en ese caso. No se baja el JWKS. Es el valor por defecto de
Andy (2026-10-28), que revisa Dan.

**Las claves, separadas.** El bridge entra a PostgREST con
``FLOW_CHANGE_APPROVER_DB_KEY``, el JWT del rol ``flow_change_approver`` de la
migracion ``20261028000100``: este modulo tiene su propio cliente y nunca usa
el de ``service_role`` ni el del Automation Expert.

El log lleva la ruta y el codigo; nunca la cookie, el ``code``, el token, el
para que ni un telefono.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid5

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from slack_correlation.catalog import NotificationCommand

from .flow_change_operator import OperatorInputError, validate_proposal
from .review_theme import REVIEW_BUTTONS_CSS, REVIEW_SHELL_CSS

__all__ = [
    "DB_ROLE",
    "PREFIX",
    "ApprovalStore",
    "FlowApprovalConfig",
    "SessionInvalid",
    "SlackIdTokenClient",
    "SlackOidcError",
    "StoreUnavailable",
    "approval_notice",
    "check_settings",
    "register_flow_approval_routes",
    "second_step_reasons",
]

logger = logging.getLogger(__name__)

PREFIX = "/flow-approval"
DB_ROLE = "flow_change_approver"
CALLBACK_PATH = f"{PREFIX}/auth/slack/callback"
SESSION_COOKIE = "__Host-flow_approval_session"
STATE_COOKIE = "__Host-flow_approval_oidc"
SESSION_HOURS = 8
STATE_MINUTES = 10
SLACK_ISSUER = "https://slack.com"
MAX_STORE_BYTES = 4 * 1024 * 1024
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{8,20}$")
_SLACK_TEAM = re.compile(r"^T[A-Z0-9]{8,20}$")
_CODE = re.compile(r"^[a-z][a-z0-9_:]{1,79}$")
_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_HMAC = re.compile(r"^[0-9a-f]{64}$")
# Lo mismo que el comando del operador: la base acepta una revalidacion de hasta 10 minutos
# (c_revalidation_window); pasados 8, se valida otra vez antes de publicar.
REVALIDATE_AFTER = timedelta(minutes=8)
# El segundo paso vale la ventana de 15 minutos en curso y la anterior (entre 15 y 30 minutos).
CONFIRM_WINDOW = timedelta(minutes=15)
_DONE_KEYS = ("hecho", "aviso", "miembros", "max")
# El motivo de un descarte, en una linea (la base acepta 1 a 500 caracteres sin control).
DISCARD_REASON_MAX = 200
# Lo que la base guarda como motivo de una pausa o una carga de cohorte desde la pagina.
PAUSE_REASON = "pausado desde la pagina de aprobacion"
COHORT_REASON = "cargado desde la pagina de aprobacion"
# El telefono, antes de pasarlo a la base (que lo canoniza): digitos y separadores.
_PHONE = re.compile(r"^\+?[0-9 ()\-.]{8,32}$")
# El aviso al canal de alertas (contrato 1.9.0, «Lo que queda registrado»).
NOTICE_PUBLISHED = "OPS-005"
NOTICE_PAUSED = "OPS-003"
_NOTICE_NAMESPACE = UUID("6c1f4b3e-2f0a-4d7e-9a51-0d6f4a8b2c19")
_BROWSER_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'")


# ---------------------------------------------------------------------------
# La configuracion.
# ---------------------------------------------------------------------------


def _jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("FLOW_APPROVAL_BASE_URL must be an https origin, without path, port or query")
    return f"https://{parsed.hostname}"


def parse_approvers(value: str) -> frozenset[str]:
    """``FLOW_APPROVAL_SLACK_APPROVERS``: IDs de Slack separados por coma."""
    approvers = [part.strip() for part in value.split(",") if part.strip()]
    if not approvers:
        raise ValueError("FLOW_APPROVAL_SLACK_APPROVERS must name at least one Slack user")
    for approver in approvers:
        if not _SLACK_USER.fullmatch(approver):
            raise ValueError("FLOW_APPROVAL_SLACK_APPROVERS has a value that is not a Slack user ID")
    return frozenset(approvers)


@dataclass(frozen=True)
class FlowApprovalConfig:
    """Lo que la pagina necesita, ya validado (``check_settings`` lo arma desde la configuracion)."""

    origin: str
    slack_team_id: str
    approvers: frozenset[str]
    client_id: str
    client_secret: str
    session_key: bytes
    db_key: str
    instance: Mapping[str, str] = field(default_factory=dict)

    @property
    def redirect_uri(self) -> str:
        return self.origin + CALLBACK_PATH

    def csrf_token(self, session_secret: str) -> str:
        return hmac.new(self.session_key, ("flow-approval-csrf-v1\0" + session_secret).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def confirm_token(self, session_secret: str, proposal_id: str, fingerprint: str,
                      reasons: list[tuple[str, str]], window: int) -> str:
        """El campo del segundo paso (otro dominio del HMAC que el CSRF).

        Atado a la sesion, a la propuesta, a su huella, a los motivos que mostro la
        pantalla (si cambia la clasificacion, vuelve a la pantalla) y a una ventana
        de ``CONFIRM_WINDOW`` (vale la ventana en curso y la anterior).
        """
        shown = "\x1f".join(f"{name}\x1e{reason}" for name, reason in sorted(reasons))
        message = "\0".join(("flow-approval-confirm-v2", session_secret, proposal_id, fingerprint, shown,
                              str(window)))
        return hmac.new(self.session_key, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def done_token(self, session_secret: str, path: str, result: Mapping[str, str]) -> str:
        """La firma del resultado del PRG: un link armado a mano no dice lo que no paso."""
        message = "\0".join(("flow-approval-done-v1", session_secret, path,
                              urlencode(sorted(result.items()))))
        return hmac.new(self.session_key, message.encode("utf-8"), hashlib.sha256).hexdigest()


_SECRET_FIELDS = (
    "flow_approval_slack_client_secret",
    "flow_approval_session_secret",
    "flow_change_approver_db_key",
)


def check_settings(settings: Any) -> FlowApprovalConfig | None:
    """Lo que exige el flag al arrancar (contrato 1.9.0, «El flag» y «La lista de aprobadores»).

    Apagado, devuelve None y no mira nada. Prendido, exige: el manifiesto de la
    instancia, Supabase, el origen https del bridge, el client ID y el secreto de
    la app de Slack del Automation Expert, el workspace, la lista de aprobadores,
    el secreto de la sesion (32 caracteres o mas) y el JWT del rol, cada secreto
    distinto de todo otro valor de la configuracion.
    """
    if not getattr(settings, "flow_approval_enabled", False):
        return None
    manifest = settings.instance_manifest
    if manifest is None:
        raise ValueError("FLOW_APPROVAL_ENABLED requires an instance manifest")
    if not settings.supabase_base_url:
        raise ValueError("FLOW_APPROVAL_ENABLED requires SUPABASE_BASE_URL")
    origin = _https_origin(settings.flow_approval_base_url or "")
    team = settings.flow_approval_slack_team_id or ""
    if not _SLACK_TEAM.fullmatch(team):
        raise ValueError("FLOW_APPROVAL_SLACK_TEAM_ID must be a Slack workspace ID (T...)")
    approvers = parse_approvers(settings.flow_approval_slack_approvers or "")
    client_id = settings.flow_approval_slack_client_id or ""
    if not re.fullmatch(r"[0-9]{6,20}\.[0-9]{6,20}", client_id):
        raise ValueError("FLOW_APPROVAL_SLACK_CLIENT_ID must be a Slack app client ID")
    client_secret = settings.flow_approval_slack_client_secret or ""
    if len(client_secret) < 16:
        raise ValueError("FLOW_APPROVAL_SLACK_CLIENT_SECRET is required")
    session_secret = settings.flow_approval_session_secret or ""
    if len(session_secret) < 32:
        raise ValueError("FLOW_APPROVAL_SESSION_SECRET must contain at least 32 characters")
    db_key = settings.flow_change_approver_db_key or ""
    claims = _jwt_claims(db_key)
    if claims is None or claims.get("role") != DB_ROLE:
        raise ValueError("FLOW_CHANGE_APPROVER_DB_KEY must be a JWT whose role is flow_change_approver")
    from dataclasses import fields as dataclass_fields

    for name in _SECRET_FIELDS:
        secret = getattr(settings, name)
        clashing = sorted(
            other.name for other in dataclass_fields(settings)
            if other.name != name and isinstance(value := getattr(settings, other.name), str) and value
            and hmac.compare_digest(secret.encode("utf-8", "surrogatepass"), value.encode("utf-8", "surrogatepass"))
        )
        if clashing:
            raise ValueError(f"{name.upper()} must differ from every other secret and value of the bridge "
                             "configuration; it equals " + ", ".join(clashing))
    return FlowApprovalConfig(
        origin=origin, slack_team_id=team, approvers=approvers, client_id=client_id,
        client_secret=client_secret, session_key=session_secret.encode("utf-8"), db_key=db_key,
        instance={"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref},
    )


# ---------------------------------------------------------------------------
# La base.
# ---------------------------------------------------------------------------


class StoreUnavailable(Exception):
    """La base no contesto o contesto algo que no se reconoce."""


class SessionInvalid(Exception):
    """La sesion o el state no sirven (28000 de la base)."""


class StoreRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApprovalStore:
    """El cliente de las funciones del rol ``flow_change_approver`` (20261028000100)."""

    def __init__(self, *, base_url: str, db_key: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._db_key = db_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def _rpc(self, function: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        headers = {"apikey": self._db_key, "Authorization": f"Bearer {self._db_key}",
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=self._transport,
                                         timeout=self._timeout_seconds) as client:
                response = await client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        except httpx.HTTPError as error:
            raise StoreUnavailable() from error
        if len(response.content) > MAX_STORE_BYTES:
            raise StoreUnavailable()
        try:
            body = response.json()
        except ValueError as error:
            raise StoreUnavailable() from error
        if response.status_code == 200 and isinstance(body, dict):
            return body
        sqlstate = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if sqlstate == "28000":
            raise SessionInvalid()
        if sqlstate in ("22023", "P0002") and isinstance(message, str) and _CODE.fullmatch(message):
            raise StoreRejected(message)
        raise StoreUnavailable()

    async def begin_oidc(self, instance: Mapping[str, str], *, state_hash: str, nonce_hash: str,
                         return_path: str, expires_at: datetime) -> dict[str, Any]:
        return await self._rpc("begin_flow_approval_oidc_v1", {
            "p_instance": instance, "p_state_hash": state_hash, "p_nonce_hash": nonce_hash,
            "p_return_path": return_path, "p_expires_at": _utc(expires_at)})

    async def consume_oidc(self, instance: Mapping[str, str], *, state_hash: str) -> dict[str, Any]:
        return await self._rpc("consume_flow_approval_oidc_v1", {"p_instance": instance, "p_state_hash": state_hash})

    async def create_session(self, instance: Mapping[str, str], *, state_hash: str, session_hash: str,
                             team_id: str, user_id: str, expires_at: datetime) -> dict[str, Any]:
        return await self._rpc("create_flow_approval_session_v1", {
            "p_instance": instance, "p_state_hash": state_hash, "p_session_hash": session_hash,
            "p_slack_team_id": team_id, "p_slack_user_id": user_id, "p_expires_at": _utc(expires_at)})

    async def validate_session(self, instance: Mapping[str, str], *, session_hash: str) -> dict[str, Any]:
        return await self._rpc("validate_flow_approval_session_v1",
                               {"p_instance": instance, "p_session_hash": session_hash})

    async def revoke_session(self, instance: Mapping[str, str], *, session_hash: str) -> dict[str, Any]:
        return await self._rpc("revoke_flow_approval_session_v1",
                               {"p_instance": instance, "p_session_hash": session_hash})

    async def get_proposal(self, instance: Mapping[str, str], *, session_hash: str,
                           proposal_id: str) -> dict[str, Any]:
        return await self._rpc("approver_get_flow_change_proposal_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_proposal_id": proposal_id})

    async def read_store(self, instance: Mapping[str, str], *, session_hash: str) -> dict[str, Any]:
        return await self._rpc("approver_read_flow_store_v1", {"p_instance": instance, "p_session_hash": session_hash})

    async def read_open(self, instance: Mapping[str, str], *, session_hash: str,
                        proposal_id: str | None) -> dict[str, Any]:
        return await self._rpc("approver_read_open_flow_changes_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_proposal_id": proposal_id})

    # -- las escrituras (A′ PR 3b). El actor lo pone la base, desde la sesion. ----

    async def approve(self, instance: Mapping[str, str], *, session_hash: str, proposal_id: str,
                      fingerprint: str) -> dict[str, Any]:
        return await self._rpc("approver_approve_flow_change_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_proposal_id": proposal_id,
            "p_fingerprint": fingerprint})

    async def publish(self, instance: Mapping[str, str], *, session_hash: str, proposal_id: str,
                      fingerprint: str, revalidation: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("approver_publish_flow_change_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_proposal_id": proposal_id,
            "p_fingerprint": fingerprint, "p_revalidation": revalidation})

    async def pause(self, instance: Mapping[str, str], *, session_hash: str, product_ref: str, flow_key: str,
                    expected_generation: int, reason: str) -> dict[str, Any]:
        return await self._rpc("approver_pause_flow_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_product_ref": product_ref,
            "p_flow_key": flow_key, "p_expected_generation": expected_generation, "p_reason": reason})

    async def discard(self, instance: Mapping[str, str], *, session_hash: str, proposal_id: str,
                      reason: str) -> dict[str, Any]:
        return await self._rpc("approver_discard_flow_change_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_proposal_id": proposal_id,
            "p_reason": reason})

    async def cohort_add(self, instance: Mapping[str, str], *, session_hash: str, product_ref: str, flow_key: str,
                         phone: str, reason: str) -> dict[str, Any]:
        return await self._rpc("approver_flow_cohort_v1", {
            "p_instance": instance, "p_session_hash": session_hash, "p_product_ref": product_ref,
            "p_flow_key": flow_key, "p_phone": phone, "p_reason": reason})


# ---------------------------------------------------------------------------
# Un clic o dos pasos (contrato 1.9.0, decision 2 del 2026-10-27).
# ---------------------------------------------------------------------------

SECOND_STEP_TEXT = {
    "llega_a_todos": "llega a todos",
    "corta_en_curso": "corta lo que está en curso",
    "apaga": "apaga el flujo",
    "sin_clasificar": "no se sabe a quién llega hasta que se prenda: se pide el segundo paso",
}


def second_step_reasons(items: object, flows: object) -> list[tuple[str, str]]:
    """Los items que piden el segundo paso, como (flujo, motivo). Vacio: un clic.

    Lo decide el bridge con los items de la propuesta (congelados por su huella)
    y el estado de cada flujo en el almacen que acaba de releer. Un clic: un
    ``nuevo`` (nace en la cohorte), un ``prender`` con ``a: cohorte`` y una
    ``version`` o un ``volver`` de un flujo en ``cohorte``. Dos pasos: pasar a
    todos, cambiar un flujo ``prendido``, ``apagar`` y cancelar lo que espera.
    Ante la duda (un tipo o un estado que no se reconoce), dos pasos.
    """
    if not isinstance(items, list) or not items:
        return [("?", "sin_clasificar")]
    by_id = {flow.get("flow_id"): flow for flow in flows if isinstance(flow, Mapping)} \
        if isinstance(flows, list) else {}
    reasons: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            reasons.append(("?", "sin_clasificar"))
            continue
        name = f"{item.get('producto')}/{item.get('flujo')}"
        tipo = item.get("tipo")
        if item.get("en_curso") not in (None, "seguir"):
            reasons.append((name, "corta_en_curso"))
        if tipo == "nuevo":
            if item.get("prender") != "cohorte":
                reasons.append((name, "sin_clasificar"))
        elif tipo == "prender":
            if item.get("a") != "cohorte":
                reasons.append((name, "llega_a_todos"))
        elif tipo == "apagar":
            reasons.append((name, "apaga"))
        elif tipo in ("version", "volver"):
            flow = by_id.get(item.get("flow_id"))
            state = flow.get("state") if isinstance(flow, Mapping) else None
            if state == "prendido":
                reasons.append((name, "llega_a_todos"))
            elif state != "cohorte":
                reasons.append((name, "sin_clasificar"))
        else:
            reasons.append((name, "sin_clasificar"))
    return reasons


# ---------------------------------------------------------------------------
# El aviso al canal de alertas.
# ---------------------------------------------------------------------------


def approval_notice(*, event_code: str, what: str, flow_key: str, user_id: str, key: str,
                    occurred_at: datetime) -> NotificationCommand:
    """Una linea al canal de alertas: el flujo, que paso y quien. Sin telefono, para que ni texto del modelo.

    Va por el catalogo cerrado del conector (OPS-005 publicado, OPS-003 pausado):
    el flujo en ``component``, que paso en ``reason_code`` y el ID de Slack de
    quien lo hizo en ``state`` (el catalogo pide minusculas). La mencion y el
    link a la pagina piden una plantilla nueva del conector: quedan para otro PR.
    ``key`` hace el aviso idempotente (un reintento del mismo hecho es el mismo aviso).
    """
    seed = "\0".join((event_code, what, flow_key, key))
    return NotificationCommand(
        event_id=str(uuid5(_NOTICE_NAMESPACE, seed)),
        event_code=event_code,
        dedupe_key=hashlib.sha256(b"flow-approval-notice-v1\0" + seed.encode("utf-8")).hexdigest(),
        occurred_at=occurred_at,
        reason_code=what,
        component=f"flow_approval:{flow_key}"[:80],
        state=f"por:{user_id.lower()}"[:80],
    )


# ---------------------------------------------------------------------------
# Slack.
# ---------------------------------------------------------------------------


class SlackOidcError(Exception):
    """El OIDC de Slack no cerro. ``reference`` es un codigo cerrado que la pagina muestra."""

    def __init__(self, reference: str) -> None:
        super().__init__(reference)
        self.reference = reference


@dataclass(frozen=True)
class SlackIdentity:
    team_id: str
    user_id: str
    nonce: str


class SlackIdTokenClient:
    """``openid.connect.token`` de Slack, y los claims del ``id_token`` validados.

    La firma no se verifica: el token llega en la respuesta TLS del endpoint de
    tokens, en el pedido que lleva el secreto de la app (OIDC Core 3.1.3.7.6).
    """

    def __init__(self, *, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._clock = clock

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        return "https://slack.com/openid/connect/authorize?" + urlencode({
            "response_type": "code", "scope": "openid", "client_id": self._client_id, "state": state,
            "nonce": nonce, "redirect_uri": redirect_uri})

    async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
        try:
            async with httpx.AsyncClient(base_url="https://slack.com", transport=self._transport,
                                         timeout=15) as client:
                response = await client.post("/api/openid.connect.token", data={
                    "code": code, "client_id": self._client_id, "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SlackOidcError("OIDC-PROVIDER-TRANSPORT") from error
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise SlackOidcError("OIDC-TOKEN-REJECTED")
        id_token = body.get("id_token")
        claims = _jwt_claims(id_token) if isinstance(id_token, str) else None
        if claims is None:
            raise SlackOidcError("OIDC-TOKEN-PAYLOAD")
        return self.identity_from_claims(claims)

    def identity_from_claims(self, claims: Mapping[str, Any]) -> SlackIdentity:
        now = self._clock()
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        expires = claims.get("exp")
        issued = claims.get("iat")
        team_id = claims.get("https://slack.com/team_id")
        user_id = claims.get("https://slack.com/user_id")
        nonce = claims.get("nonce")
        if claims.get("iss") != SLACK_ISSUER:
            raise SlackOidcError("OIDC-ISSUER")
        if self._client_id not in audiences or (isinstance(audience, list) and claims.get("azp")
                                                not in (None, self._client_id)):
            raise SlackOidcError("OIDC-AUDIENCE")
        if (not isinstance(expires, int | float) or isinstance(expires, bool) or expires <= now - 60
                or (isinstance(issued, int | float) and not isinstance(issued, bool) and issued > now + 300)):
            raise SlackOidcError("OIDC-EXPIRED")
        if (not isinstance(team_id, str) or not _SLACK_TEAM.fullmatch(team_id)
                or not isinstance(user_id, str) or not _SLACK_USER.fullmatch(user_id)
                or claims.get("sub") != user_id):
            raise SlackOidcError("OIDC-IDENTITY-PAYLOAD")
        if not isinstance(nonce, str) or not nonce:
            raise SlackOidcError("OIDC-NONCE")
        return SlackIdentity(team_id=team_id, user_id=user_id, nonce=nonce)


# ---------------------------------------------------------------------------
# Las paginas. Todo lo que viene de la base pasa por html.escape.
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# La estetica es la de la revision diaria (bridge.review_theme, que copia Chatwoot): tokens, barra de
# arriba y botones compartidos; lo de abajo es lo propio de esta pagina, con los mismos tokens.
_PAGE_CSS = (
    "main.app-frame{width:min(1080px,calc(100% - 40px));margin:0 auto;padding:28px 0 48px}"
    "h1,h2,h3,p{margin-top:0}h1{font-size:24px;line-height:1.25;letter-spacing:-.025em;margin-bottom:6px}"
    "h2{font-size:16px;line-height:1.35;letter-spacing:-.01em;margin-bottom:4px}"
    "h3{font-size:12px;color:var(--cw-subtle);letter-spacing:.06em;text-transform:uppercase;margin:0 0 6px}"
    ".meta,.muted{color:var(--cw-muted)}.meta{font-size:13px}a{color:#64a8f0}"
    ".page-header{margin-bottom:20px}.page-header .meta{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;"
    "margin:0}.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--cw-subtle);"
    "overflow-wrap:anywhere}\n"
    ".layout{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(300px,.8fr);gap:16px;align-items:start}"
    ".stack{display:grid;gap:16px;min-width:0}.panel{min-width:0;background:var(--cw-surface);"
    "border:1px solid var(--cw-border-soft);border-radius:var(--cw-radius);overflow:hidden}"
    ".panel-heading{padding:16px 18px;border-bottom:1px solid var(--cw-border-soft)}.panel-heading p{margin:0;"
    "font-size:13px;color:var(--cw-muted)}.panel-body{padding:16px 18px}.panel-body>:last-child{margin-bottom:0}"
    ".panel--side{position:sticky;top:80px}.panel-heading--split{border-top:1px solid var(--cw-border-soft)}\n"
    ".summary-grid{display:grid;grid-template-columns:1fr 1fr;background:var(--cw-sidebar)}"
    ".summary-item{padding:14px 18px;min-width:0;border-top:1px solid var(--cw-border-soft)}"
    ".summary-item:nth-child(-n+2){border-top:0}.summary-item:nth-child(2n){border-left:1px solid var(--cw-border-soft)}"
    ".summary-label{display:block;color:var(--cw-subtle);font-size:11px;font-weight:650;letter-spacing:.06em;"
    "text-transform:uppercase;margin-bottom:5px}.summary-value{color:var(--cw-text);font-size:14px;overflow-wrap:anywhere}"
    ".summary-value small{display:block;color:var(--cw-subtle);font-size:12px}\n"
    ".chip{display:inline-block;border:1px solid var(--cw-border);border-radius:999px;padding:1px 9px;font-size:12px;"
    "color:var(--cw-muted);white-space:nowrap}.chip--ok{border-color:#2f7a55;color:#9fe3c0}"
    ".chip--warn{border-color:#8a5a1f;color:#f0c27b}.chip--info{border-color:#24548a;color:#9cc8f5}\n"
    ".thread{padding:20px 18px;display:flex;flex-direction:column;gap:14px;background:var(--cw-bg)}"
    ".message{align-self:flex-end;max-width:86%;min-width:0}.actor{color:var(--cw-subtle);font-size:11px;font-weight:600;"
    "margin:0 3px 4px;text-align:right;overflow-wrap:anywhere}.bubble{padding:11px 14px;border-radius:var(--cw-radius);"
    "border-bottom-right-radius:4px;background:var(--cw-indigo);color:#f3f3ff;white-space:pre-wrap;overflow-wrap:anywhere}"
    ".bubble-head{font-weight:700;margin-bottom:6px}.bubble-foot{color:#c4c5ef;font-size:12px;margin-top:8px}"
    ".bubble-button{display:block;margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.16);"
    "text-align:center;color:#cfe3ff;font-size:13px;font-weight:600;white-space:normal}"
    ".bubble--missing{background:var(--cw-surface-raised);border:1px dashed #4a4c58;color:var(--cw-muted)}"
    ".event{align-self:center;color:var(--cw-muted);font-size:12px;text-align:center;padding:3px 12px;border-radius:999px;"
    "background:#1a1b20;border:1px solid var(--cw-border-soft)}.branch{align-self:stretch;display:flex;"
    "flex-direction:column;gap:12px;border-left:2px solid var(--cw-border);padding-left:12px}"
    ".branch-label{color:var(--cw-subtle);font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase}\n"
    ".note{border:1px dashed #4a4c58;border-radius:var(--cw-radius-sm);padding:10px 12px;color:#d6d7dc;font-size:14px;"
    "background:#1a1b20;white-space:pre-wrap;overflow-wrap:anywhere}"
    ".notice{border:1px solid var(--cw-border);background:var(--cw-surface-raised);border-radius:var(--cw-radius-sm);"
    "padding:12px 14px;font-size:14px;overflow-wrap:anywhere}.notice p:last-child,.notice ul:last-child{margin-bottom:0}"
    ".notice--ok{border-color:#2f7a55;background:#14241c;color:#d5f3e3}"
    ".notice--warn{border-color:#8a5a1f;background:#261f14;color:#f3dcb4}.notice ul{padding-left:18px}\n"
    ".actions{display:grid;gap:16px;padding:18px}.actions form{display:grid;gap:8px;margin:0}"
    ".actions .divider{border-top:1px solid var(--cw-border-soft)}label{font-size:13px;font-weight:600;display:grid;gap:6px}"
    "input[type=text],input[type=tel]{width:100%;min-height:40px;padding:9px 12px;border:1px solid #353740;"
    "border-radius:var(--cw-radius-sm);background:#121317;color:var(--cw-text);font:inherit;font-weight:400}"
    "input[type=text]:focus,input[type=tel]:focus{border-color:var(--cw-accent);box-shadow:0 0 0 3px rgba(25,118,210,.2);"
    "outline:0}ul.plain{margin:0;padding-left:18px;font-size:14px}ul.plain li{margin:3px 0;overflow-wrap:anywhere}\n"
    "details.tech>summary{cursor:pointer;padding:14px 18px;color:var(--cw-muted);font-size:13px;font-weight:600}"
    "details.tech[open]>summary{border-bottom:1px solid var(--cw-border-soft)}.tech-body{padding:16px 18px;display:grid;"
    "gap:16px;min-width:0}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px}"
    "td,th{border-top:1px solid var(--cw-border-soft);padding:6px 8px;text-align:left;vertical-align:top}"
    "th{color:var(--cw-subtle);font-size:11px;font-weight:650;letter-spacing:.04em;text-transform:uppercase}"
    "code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}"
    "pre{white-space:pre-wrap;margin:0;overflow-wrap:anywhere;color:#d4d5db}\n"
    ".who{display:flex;align-items:center;gap:10px;margin:0;min-width:0}.who-text{color:var(--cw-muted);font-size:12px;"
    "overflow-wrap:anywhere}.who-text code{color:var(--cw-text);font-size:12px}.who button{min-height:32px;padding:6px 12px;"
    "font-size:13px}\n"
    ".row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 16px;align-items:center;padding:14px 18px;"
    "border-top:1px solid var(--cw-border-soft)}.row:first-child{border-top:0}.row-name{font-weight:650}"
    ".row-sub{color:var(--cw-subtle);font-size:12px;overflow-wrap:anywhere}.row-forms{display:flex;flex-wrap:wrap;gap:8px;"
    "justify-content:flex-end}.row-forms form{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:0}"
    "a.row{color:var(--cw-text);text-decoration:none}a.row:hover{background:var(--cw-hover)}"
    ".empty{padding:16px 18px;color:var(--cw-muted);font-size:14px}\n"
    ".auth{max-width:460px;margin:9vh auto 0}.confirm{max-width:560px}.auth .panel-body{display:grid;gap:14px}.auth .button{width:100%}\n"
    "@media(max-width:880px){.layout{grid-template-columns:1fr}.panel--side{position:static}}"
    "@media(max-width:620px){.topbar{height:58px;padding:0 16px}.brand-name,.secure-badge{display:none}"
    "main.app-frame{width:min(100% - 24px,1080px);padding-top:18px}.summary-grid{grid-template-columns:1fr}"
    ".summary-item:nth-child(2n){border-left:0}.summary-item:nth-child(2){border-top:1px solid var(--cw-border-soft)}"
    ".row{grid-template-columns:1fr}.row-forms{justify-content:flex-start}.message{max-width:96%}"
    ".thread{padding:16px 12px}}@media(max-width:420px){.who-text{display:none}}"
)
BRAND_NAME = "Recuperador"


def _shell(title: str, body: str, *, user: str | None = None, csrf: str | None = None) -> str:
    # Quien entro: el bridge pide solo el scope openid, asi que de la sesion sabe el id de Slack y no el
    # nombre (los claims name/given_name vienen con el scope profile). Se muestra el id.
    right = '<span class="secure-badge">Acceso con Slack</span>'
    if user is not None and csrf is not None:
        right = (f'<form method="post" action="{PREFIX}/logout" class="who"><span class="who-text">Entraste como '
                 f'<code>{_esc(user)}</code></span><input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
                 '<button type="submit">Salir</button></form>')
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · Aprobación de flujos</title><style>
{REVIEW_SHELL_CSS}{REVIEW_BUTTONS_CSS}
{_PAGE_CSS}
</style></head><body class="app-shell"><div class="topbar"><div class="brand"><div class="workspace-mark" aria-hidden="true"></div><span class="brand-name">{_esc(BRAND_NAME)}</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-section">Aprobación de flujos</span></div>{right}</div><main class="app-frame">{body}</main></body></html>'''


def _page(title: str, message: str, status: int) -> HTMLResponse:
    body = (f'<section class="auth panel"><div class="panel-heading"><h2>{_esc(title)}</h2></div>'
            f'<div class="panel-body"><p>{_esc(message)}</p></div></section>')
    return HTMLResponse(_shell(title, body), status_code=status)


def _login(return_to: str | None) -> HTMLResponse:
    query = f"?{urlencode({'p': return_to})}" if return_to else ""
    body = ('<section class="auth panel"><div class="panel-heading"><h2>Aprobar cambios de flujos</h2>'
            "<p>Acá ves lo que el agente propone cambiar en los mensajes automáticos y decidís si sale.</p></div>"
            '<div class="panel-body"><p>Para ver la propuesta, entrá con tu cuenta de Slack.</p>'
            f'<a class="button primary" href="{PREFIX}/auth/slack/start{_esc(query)}">Entrar con Slack</a>'
            '<p class="meta">Solo entran las personas de la lista de aprobadores de esta instancia.</p></div></section>')
    return HTMLResponse(_shell("Entrar", body), status_code=401)


_STATUS = {
    "borrador": "borrador (todavía no validada)", "validada": "validada, esperando el sí", "aprobada": "aprobada, sin publicar",
    "publicada": "publicada", "descartada": "descartada", "vencida": "vencida",
}
_STATUS_CHIP = {"validada": "chip--info", "aprobada": "chip--warn", "publicada": "chip--ok", "vencida": "chip--warn"}
_A_QUIEN = {"cohorte": "solo la cohorte de prueba", None: "lo que ya alcanza cada flujo"}

# El resumen en lenguaje de negocio (E-232, D1). Lo tecnico queda abajo, plegado.
_TIPO = {"nuevo": "Flujo nuevo", "version": "Cambio del flujo", "prender": "Prender el flujo",
         "apagar": "Apagar el flujo", "volver": "Volver a una versión anterior"}
_EVENTO = {
    "carrito": "cuando alguien deja el carrito sin pagar",
    "pago_fallido": "cuando a alguien le rechazan el pago",
    "ficha_impresa": "cuando alguien genera una ficha para pagar en efectivo",
    "ficha_vencida": "cuando una ficha de pago en efectivo vence sin pagarse",
    "compra": "cuando alguien compra",
    "intencion": "cuando alguien muestra intención de compra en la conversación",
}
_QUIEN_PIDE = {"automation_expert": "el agente (Automation Expert)", "operador": "el operador"}
_CATEGORIA = {"UTILITY": "utilidad", "MARKETING": "marketing", "AUTHENTICATION": "autenticación"}
_ESTADO_PLANTILLA = {"APPROVED": "aprobada por Meta", "PENDING": "esperando a Meta", "REJECTED": "rechazada por Meta",
                     "PAUSED": "pausada por Meta", "DISABLED": "desactivada por Meta"}
_ESTADO_FLUJO = {"prendido": "prendido, le llega a todos", "cohorte": "en prueba, solo la cohorte",
                 "pausado": "pausado", "apagado": "apagado"}
_ESTADO_CHIP = {"prendido": "chip--ok", "cohorte": "chip--info", "pausado": "chip--warn"}
_UNIT = {"m": ("minuto", "minutos"), "h": ("hora", "horas"), "d": ("día", "días")}
_DURATION = re.compile(r"^([0-9]{1,4})([mhd])$")


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _chip(text: object, kind: str | None = None) -> str:
    return f'<span class="chip{" " + kind if kind else ""}">{_esc(text)}</span>'


def _duration(value: object) -> str:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return _esc(value)
    count = int(match.group(1))
    one, many = _UNIT[match.group(2)]
    return f"{count} {one if count == 1 else many}"


def _flow_name(producto: object, flujo: object) -> str:
    return f"{producto}/{flujo}"


def _definition_for(item: Mapping[str, Any], flows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """La definicion que queda publicada con el item: la del item, la de la version a la que vuelve o la activa."""
    if isinstance(item.get("definicion"), Mapping):
        return item["definicion"]
    flow = next((f for f in flows if f.get("product_ref") == item.get("producto")
                 and f.get("flow_key") == item.get("flujo")), None)
    versions = flow.get("versions") if isinstance(flow, Mapping) and isinstance(flow.get("versions"), Mapping) else {}
    wanted = item.get("version") if item.get("tipo") == "volver" else (flow or {}).get("active_version")
    found = versions.get(str(wanted)) if wanted is not None else None
    return found if isinstance(found, Mapping) else None


def _trigger_text(definition: Mapping[str, Any]) -> str:
    trigger = definition.get("dispara")
    if not isinstance(trigger, Mapping):
        return "sin disparador"
    if "silencio" in trigger:
        return "cuando una conversación queda en silencio"
    event = trigger.get("evento")
    return _esc(_EVENTO.get(str(event), f"con el evento {event}"))


def _schedule_text(definition: Mapping[str, Any]) -> str:
    schedule = definition.get("horario")
    if schedule == "siempre":
        return "a cualquier hora"
    if isinstance(schedule, Mapping):
        return (f"de {_esc(schedule.get('desde'))} a {_esc(schedule.get('hasta'))}"
                f"<small>hora de {_esc(schedule.get('zona'))}</small>")
    return _esc(schedule) if schedule is not None else "—"


def _end_text(definition: Mapping[str, Any]) -> str:
    ends = definition.get("termina") if isinstance(definition.get("termina"), Mapping) else {}
    parts = []
    if ends.get("vence") is not None:
        parts.append(f"a {_duration(ends.get('vence'))} de arrancar")
    reasons = [str(reason) for reason in ends.get("si") or [] if isinstance(reason, str)]
    if reasons:
        parts.append("o antes si pasa: " + _esc(", ".join(reasons)))
    return " ".join(parts) if parts else "—"


def _message_bubble(message: Mapping[str, Any] | None, step: Mapping[str, Any]) -> str:
    name = step.get("plantilla")
    if isinstance(name, Mapping):
        actor = f"Plantilla a crear: {_esc(name.get('a_crear'))}"
    else:
        actor = f"Plantilla {_esc(name)}"
    if message is not None and isinstance(message.get("categoria"), str):
        actor += (f" · {_esc(_CATEGORIA.get(message['categoria'], message['categoria']))}"
                  f" · {_esc(_ESTADO_PLANTILLA.get(str(message.get('estado')), message.get('estado')))}")
    seen = message.get("como_lo_ve") if isinstance(message, Mapping) else None
    if not isinstance(seen, Mapping):
        why = ("Todavía no existe en el catálogo de la bandeja." if isinstance(name, Mapping) or (
            message is not None and message.get("encontrada") is False)
               else "Esta propuesta no trae la vista previa del texto.")
        return (f'<div class="message"><p class="actor">{actor}</p>'
                f'<div class="bubble bubble--missing">{_esc(why)}</div></div>')
    inner = ""
    if seen.get("encabezado"):
        inner += f'<div class="bubble-head">{_esc(seen.get("encabezado"))}</div>'
    inner += _esc(seen.get("cuerpo") or "")
    if seen.get("pie"):
        inner += f'<div class="bubble-foot">{_esc(seen.get("pie"))}</div>'
    for button in seen.get("botones") or []:
        if isinstance(button, Mapping):
            inner += f'<span class="bubble-button">{_esc(button.get("texto"))}</span>'
    return f'<div class="message"><p class="actor">{actor}</p><div class="bubble">{inner}</div></div>'


def _render_steps(steps: object, at: str, messages: Mapping[str, Mapping[str, Any]], depth: int = 0) -> str:
    """El recorrido como en un chat: los mensajes como burbuja, y las esperas y etiquetas como eventos."""
    if not isinstance(steps, list):
        return ""
    out = ""
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        path = f"{at}/{index}"
        if "plantilla" in step:
            out += _message_bubble(messages.get(path), step)
        elif "esperar" in step:
            out += f'<div class="event">Espera {_duration(step.get("esperar"))}</div>'
        elif "etiquetar" in step:
            out += f'<div class="event">Le pone la etiqueta «{_esc(step.get("etiquetar"))}»</div>'
        elif "conversar" in step:
            out += '<div class="event">Conversa el agente</div>'
        elif "derivar" in step:
            out += '<div class="event">Pasa la conversación a una persona del equipo</div>'
        elif "condicion" in step and depth < 4:
            out += '<div class="event">Según una condición (el detalle, en «Detalle técnico»)</div>'
            for branch, label in (("entonces", "Si se cumple"), ("si_no", "Si no")):
                inner = _render_steps(step.get(branch), f"{path}/{branch}", messages, depth + 1)
                if inner:
                    out += f'<div class="branch"><span class="branch-label">{label}</span>{inner}</div>'
        else:
            out += '<div class="event">Un paso más (el detalle, en «Detalle técnico»)</div>'
    return out


def _render_item_summary(item: Mapping[str, Any], change: Mapping[str, Any] | None,
                         definition: Mapping[str, Any] | None, a_quien: str) -> str:
    tipo = item.get("tipo")
    what = _TIPO.get(str(tipo), str(tipo))
    if tipo == "version" and change is not None and change.get("base_version") is not None:
        what += f"<small>versión nueva sobre la {_esc(change.get('base_version'))}</small>"
    elif tipo == "volver":
        what += f"<small>vuelve a la versión {_esc(item.get('version'))}</small>"
    elif tipo == "nuevo":
        what += "<small>nace en la cohorte de prueba</small>"
    state = change.get("estado") if change is not None and isinstance(change.get("estado"), Mapping) else None
    name = definition.get("nombre") if definition is not None and definition.get("nombre") else None
    cells = [("Qué flujo", (_esc(name) if name else _esc(_flow_name(item.get("producto"), item.get("flujo"))))
              + f"<small>{_esc(_flow_name(item.get('producto'), item.get('flujo')))}</small>"),
             ("Qué cambia", what)]
    if state is not None:
        cells.append(("Estado", f"{_esc(_ESTADO_FLUJO.get(str(state.get('antes')), state.get('antes')))} → "
                                f"{_esc(_ESTADO_FLUJO.get(str(state.get('despues')), state.get('despues')))}"))
    cells.append(("A quién llega", _esc(a_quien)))
    if definition is not None:
        cells += [("Cuándo arranca", _trigger_text(definition)), ("En qué horario", _schedule_text(definition)),
                  ("Cuándo termina", _end_text(definition))]
    return '<div class="summary-grid">' + "".join(
        f'<div class="summary-item"><span class="summary-label">{label}</span><div class="summary-value">{value}</div>'
        "</div>" for label, value in cells) + "</div>"


def _render_change(change: Mapping[str, Any]) -> str:
    """El diff tecnico campo por campo de un item (va dentro de «Detalle técnico»)."""
    head = (f"<h3>{_esc(change.get('producto'))}/{_esc(change.get('flujo'))} · {_esc(change.get('tipo'))}")
    if change.get("base_version") is not None:
        head += f" · sobre la versión {_esc(change.get('base_version'))}"
    if change.get("nace_en"):
        head += f" · nace en {_esc(change.get('nace_en'))}"
    head += "</h3>"
    state = change.get("estado")
    if isinstance(state, Mapping):
        head += f"<p>Estado: {_esc(state.get('antes'))} → {_esc(state.get('despues'))}</p>"
    if change.get("en_curso"):
        head += f"<p>Lo que está en curso: {_esc(change.get('en_curso'))}</p>"
    rows = change.get("diff")
    if isinstance(rows, list) and rows:
        body = "".join(
            f"<tr><td><code>{_esc(row.get('ruta'))}</code></td><td><pre>{_esc(_json_text(row.get('antes')))}</pre></td>"
            f"<td><pre>{_esc(_json_text(row.get('despues')))}</pre></td></tr>"
            for row in rows if isinstance(row, Mapping))
        head += ('<div class="table-wrap"><table><thead><tr><th>Campo</th><th>Antes</th><th>Después</th></tr></thead>'
                 f"<tbody>{body}</tbody></table></div>")
    elif change.get("diff_count"):
        head += f"<p class=\"muted\">{_esc(change.get('diff_count'))} cambios, demasiado largos para mostrar.</p>"
    return f"<div>{head}</div>"


def _simulation_summary(simulation: object, *, turns_off: bool = False) -> str:
    gate = ("Apagar solo saca envíos: la página lo publica igual." if turns_off else
            "La página no publica sin simulación: la publica el operador con <code>--sin-simulacion</code>.")
    if not isinstance(simulation, Mapping):
        return f"<p>Sin simulación construida. {gate}</p>"
    if simulation.get("status") != "built":
        return (f"<p>La simulación no se pudo armar (<code>{_esc(simulation.get('reason_code'))}</code>). "
                f"{gate}</p>")
    summary = simulation.get("summary") if isinstance(simulation.get("summary"), Mapping) else {}
    line = ""
    fired, people = summary.get("disparos"), summary.get("personas")
    if isinstance(fired, int) and isinstance(people, int):
        line = (f"<p>En los últimos 7 días se habría disparado <strong>{_esc(fired)}</strong> "
                f"{'vez' if fired == 1 else 'veces'}, para <strong>{_esc(people)}</strong> "
                f"{'persona' if people == 1 else 'personas'}.</p>")
    return (line + "<p class=\"meta\">Estimación con los eventos reales de los últimos 7 días: no sabe qué habría "
            "contestado alguien a un mensaje que no salió.</p>")


def _render_simulation(simulation: object) -> str:
    """La simulacion tal cual, para el detalle tecnico."""
    if not isinstance(simulation, Mapping) or simulation.get("status") != "built":
        return ""
    summary = {key: value for key, value in simulation.items() if key not in ("status", "summary_sha256")}
    return f"<div><h3>Simulación</h3><pre>{_esc(_json_text(summary))}</pre></div>"


def _render_proposal(proposal: Mapping[str, Any], others: list[Mapping[str, Any]], *, actions: str = "",
                     done: Mapping[str, str] | None = None, flows: list[Mapping[str, Any]] | None = None) -> str:
    status = proposal.get("effective_status") or proposal.get("status")
    validation = proposal.get("validation") if isinstance(proposal.get("validation"), Mapping) else {}
    preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
    items = [item for item in proposal.get("items") or [] if isinstance(item, Mapping)]
    changes = [change for change in preview.get("cambios") or [] if isinstance(change, Mapping)]
    flows = flows or []
    messages = {}
    for message in preview.get("mensajes") or []:
        if isinstance(message, Mapping) and isinstance(message.get("ruta"), str):
            messages[(message.get("producto"), message.get("flujo"), message["ruta"])] = message
    a_quien = _A_QUIEN.get(preview.get("a_quien"), preview.get("a_quien"))
    # El titulo: que flujo y que pasa, como lo diria el duenio.
    titles = []
    for item in items:
        definition = _definition_for(item, flows)
        name = (definition or {}).get("nombre") or _flow_name(item.get("producto"), item.get("flujo"))
        titles.append(f"{_TIPO.get(str(item.get('tipo')), item.get('tipo'))}: {name}")
    title = "; ".join(titles) if titles else "Propuesta de cambio de flujos"
    header = (f'<header class="page-header"><p class="meta">Propuesta de cambio de flujos</p><h1>{_esc(title)}</h1>'
              f'<p class="meta">{_chip(_STATUS.get(str(status), status), _STATUS_CHIP.get(str(status)))}'
              f"<span>pedida por {_esc(_QUIEN_PIDE.get(str(proposal.get('requested_by_kind')), proposal.get('requested_by_kind')))}</span>"
              f'<span class="id">{_esc(proposal.get("proposal_id"))}</span></p></header>')
    main: list[str] = []
    done_html = _render_done(done or {})
    if done_html:
        main.append(done_html)
    if others:
        listed = "".join(
            f"<li>{_esc(', '.join(f'{flow.get('producto')}/{flow.get('flujo')}' for flow in other.get('flows') or []))}"
            f" · {_esc(_STATUS.get(str(other.get('status')), other.get('status')))}"
            f" · validada {_esc(other.get('validated_at') or 'sin validar')}</li>"
            for other in others)
        main.append('<div class="notice notice--warn"><p><strong>Hay otra propuesta abierta de este flujo.</strong> Si '
                    f"publicás esta, la otra va a tener que validarse de nuevo.</p><ul>{listed}</ul></div>")
    if status == "vencida":
        main.append('<div class="notice notice--warn"><p>Esta propuesta venció: pasaron 24 horas desde que el agente '
                    "la validó. Para seguir, pedile al agente que la valide de nuevo y te deja otro botón.</p></div>")
    purpose = preview.get("para_que", proposal.get("purpose"))
    by_flow = {(change.get("producto"), change.get("flujo")): change for change in changes}
    for item in items:
        key = (item.get("producto"), item.get("flujo"))
        definition = _definition_for(item, flows)
        section = ('<section class="panel"><div class="panel-heading"><h2>Resumen</h2>'
                   "<p>Qué flujo es, qué cambia y a quién le llega.</p></div>"
                   + _render_item_summary(item, by_flow.get(key), definition, str(a_quien)))
        if definition is not None and item.get("tipo") in ("nuevo", "version", "volver"):
            own = {path: message for (producto, flujo, path), message in messages.items()
                   if (producto, flujo) == key}
            steps = _render_steps(definition.get("pasos"), "/pasos", own)
            if steps:
                section += ('<div class="panel-heading panel-heading--split"><h2>Lo que va a recibir la persona</h2>'
                            "<p>Los mensajes como los ve en WhatsApp; entre corchetes, lo que se completa con sus "
                            "datos.</p></div>"
                            f'<div class="thread">{steps}</div>')
        main.append(section + "</section>")
    if not items:
        main.append('<section class="panel"><div class="panel-body"><p class="muted">La propuesta no tiene vista '
                    "previa guardada.</p></div></section>")
    main.append('<section class="panel"><div class="panel-heading"><h2>Para qué</h2>'
                "<p>Texto de quien propuso, sin verificar.</p></div>"
                f'<div class="panel-body"><div class="note">{_esc(purpose)}</div></div></section>')
    impact = ["<h3>Simulación</h3>" + _simulation_summary(preview.get("simulacion"),
                                                                turns_off=only_turns_off(proposal.get("items")))]
    in_flight = preview.get("en_curso")
    if isinstance(in_flight, Mapping) and in_flight.get("politica") is not None:
        count = in_flight.get("recorridos")
        impact.append(f"<h3>Lo que está en curso</h3><p>Política: {_esc(in_flight.get('politica'))}"
                      + (f" · {_esc(count)} recorridos" if count is not None else " · sin contar") + "</p>")
    back = preview.get("volver_atras")
    if isinstance(back, list) and back:
        lines = "".join(
            f"<li>{_esc(row.get('producto'))}/{_esc(row.get('flujo'))}: "
            + (f"vuelve a la versión {_esc(row.get('a_version'))}" if row.get("a_version") is not None
               else "es nuevo: se pausa o se apaga") + "</li>"
            for row in back if isinstance(row, Mapping))
        impact.append("<h3>Cómo se vuelve atrás</h3><p>Se pide en el DM del agente («volvé a la versión "
                      f'anterior») y llega con su propio botón.</p><ul class="plain">{lines}</ul>')
    warnings = [warning for warning in preview.get("advertencias") or [] if isinstance(warning, Mapping)]
    if warnings:
        impact.append(f"<h3>Advertencias</h3><p>El validador dejó {len(warnings)} "
                      f"{'advertencia' if len(warnings) == 1 else 'advertencias'}: están en «Detalle técnico».</p>")
    main.append('<section class="panel"><div class="panel-heading"><h2>Antes de decidir</h2></div>'
                f'<div class="panel-body">{"".join(impact)}</div></section>')
    tech = [f"<div><h3>La propuesta</h3><p><code>{_esc(proposal.get('proposal_id'))}</code> · pedida por "
            f"{_esc(proposal.get('requested_by_kind'))} · estado <code>{_esc(status)}</code></p></div>"]
    tech += [_render_change(change) for change in changes]
    if not changes:
        tech.append("<p class=\"muted\">La propuesta no tiene vista previa guardada.</p>")
    tech.append(_render_simulation(preview.get("simulacion")))
    if warnings:
        tech.append("<div><h3>Advertencias</h3><ul class=\"plain\">" + "".join(
            f"<li><code>{_esc(warning.get('code'))}</code> {_esc(warning.get('path') or '')}</li>"
            for warning in warnings) + "</ul></div>")
    gaps = [gap for gap in preview.get("huecos") or [] if isinstance(gap, Mapping)]
    if gaps:
        tech.append("<div><h3>Lo que la vista previa todavía no sabe</h3><ul class=\"plain\">" + "".join(
            f"<li><code>{_esc(gap.get('code'))}</code></li>" for gap in gaps) + "</ul></div>")
    main.append('<section class="panel"><details class="tech"><summary>Detalle técnico (campo por campo)</summary>'
                f'<div class="tech-body">{"".join(tech)}</div></details></section>')
    side = (f'<aside class="panel panel--side"><div class="panel-heading"><h2>Tu decisión</h2>'
            "<p>Publicar hace que el cambio empiece a correr.</p></div>"
            f'<div class="actions">{actions}<p class="meta"><a href="{PREFIX}/">Ver todos los flujos</a></p></div>'
            "</aside>")
    return f'{header}<div class="layout"><div class="stack">{"".join(main)}</div>{side}</div>'


# -- los botones (A′ PR 3b) -------------------------------------------------------

# Lo que dice la pagina despues de escribir (PRG: ?hecho=<codigo>). Un codigo que no esta
# aca se muestra como codigo, escapado.
_DONE = {
    "publicada": "Publicada.",
    "ya_publicada": "Ya estaba publicada: no se volvió a publicar.",
    "descartada": "Pedido descartado: el flujo quedó como estaba, sin ningún cambio.",
    "pausado": "Pausado. No arranca recorridos nuevos; los que estaban en curso esperan sin mandar.",
    "ya_pausado": "Ya estaba pausado.",
    "generacion": "El flujo cambió mientras mirabas: no se pausó. Mirá su estado de ahora.",
    "cohorte": "Teléfono cargado en la cohorte.",
    "cohorte_ya_estaba": "Ese teléfono ya estaba en la cohorte.",
    "flow_change_stale_base": "No se publicó: cambió la base de la propuesta (se publicó otra cosa o se pausó el "
                              "flujo). No hay reintento que sirva: pedile al agente una propuesta nueva.",
    "flow_change_proposal_expired": "No se publicó: la propuesta venció. Pedile al agente que la valide de nuevo.",
    "flow_change_not_approved": "No se publicó: la propuesta no está validada ni aprobada.",
    "flow_change_revalidation_blocked": "No se publicó: la base no aceptó la revalidación. Probá reintentar.",
    "revalidation_blocked": "No se publicó: la validación de ahora tiene bloqueos. Pedile al agente que la revise.",
    "template_catalog_unavailable": "No se publicó: el catálogo de plantillas no contestó. Probá reintentar.",
    "validator_unavailable": "No se publicó: el validador no pudo correr. Probá en un rato.",
    "simulation_not_built": "No se publica sin simulación desde la página: la publica el operador con "
                            "--sin-simulacion.",
    "flow_cohort_full": "No se cargó: la cohorte ya tiene el máximo de teléfonos.",
    "flow_cohort_needs_native_flow": "No se cargó: la cohorte es solo de un flujo nativo.",
    "flow_cohort_contact_not_found": "No se cargó: no hay un contacto con ese teléfono.",
    "flow_cohort_contact_ambiguous": "No se cargó: hay más de un contacto con ese teléfono.",
    "flow_cohort_phone_invalid": "No se cargó: el teléfono no tiene forma de teléfono.",
    "flow_pause_requires_on": "No se pausó: el flujo está apagado.",
    "flow_not_found": "No existe ese flujo en esta instancia.",
    "flow_change_final": "No se descartó: la propuesta ya estaba cerrada.",
    "flow_store_text_invalid": "El motivo no puede estar vacío ni tener caracteres de control.",
}
_NOTICE = {
    "enviado": "Se avisó en el canal de alertas.",
    "no_salio": "El aviso al canal de alertas no salió; lo hecho no se deshace.",
    "sin_conector": "Esta instancia no tiene conector de Slack: no se avisó en el canal de alertas.",
}


def cohort_count_text(members: int, maximum: int) -> str:
    """Cuantos telefonos tiene la cohorte y cuantos lugares le quedan (E-235, 1).

    ``members`` son los cargados, no los que faltan: con 1 de 5 quedan 4 lugares.
    """
    left = max(maximum - members, 0)
    loaded = f"{members} de {maximum} {'teléfono cargado' if maximum == 1 else 'teléfonos cargados'}"
    if left == 0:
        return f"{loaded} · no quedan lugares."
    return f"{loaded} · {'queda 1 lugar' if left == 1 else f'quedan {left} lugares'}."


def only_turns_off(items: object) -> bool:
    """Si la propuesta solo apaga flujos: todos sus items son ``apagar`` (E-235, 3).

    Apagar solo saca envios, asi que la pagina lo publica sin simulacion. Lo
    demas (un flujo nuevo, una version, volver, prender) la sigue necesitando.
    Ante la duda (sin items, un item que no es un objeto), no.
    """
    return isinstance(items, list) and bool(items) and all(
        isinstance(item, Mapping) and item.get("tipo") == "apagar" for item in items)


def _hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{_esc(name)}" value="{_esc(value)}">'


def _render_done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    text = _DONE.get(code)
    body = _esc(text) if text else f"No se hizo: <code>{_esc(code)}</code>."
    members, maximum = done.get("miembros"), done.get("max")
    if code in ("cohorte", "cohorte_ya_estaba") and members and maximum:
        body += f" {_esc(cohort_count_text(int(members), int(maximum)))}"
    notice = _NOTICE.get(done.get("aviso") or "")
    if notice:
        body += f" {_esc(notice)}"
    ok = code in ("publicada", "ya_publicada", "descartada", "pausado", "ya_pausado", "cohorte", "cohorte_ya_estaba")
    return f'<div class="notice notice--{"ok" if ok else "warn"}" role="status"><p>{body}</p></div>'


def _flow_forms(flow: Mapping[str, Any], csrf: str, return_to: str | None) -> str:
    """Pausar (prendido o cohorte) y sumar a la cohorte (cohorte), para un flujo del almacen."""
    product, key, state = flow.get("product_ref"), flow.get("flow_key"), flow.get("state")
    if not (isinstance(product, str) and isinstance(key, str)):
        return ""
    back = _hidden("p", return_to) if return_to else ""
    out = ""
    if state in ("prendido", "cohorte") and isinstance(flow.get("generation"), int):
        out += (f'<form method="post" action="{PREFIX}/pause">{_hidden("csrf_token", csrf)}'
                f'{_hidden("producto", product)}{_hidden("flujo", key)}{_hidden("generacion", flow["generation"])}'
                f'{back}<button type="submit">Pausar {_esc(product)}/{_esc(key)}</button></form>')
    if state == "cohorte":
        out += (f'<form method="post" action="{PREFIX}/cohort">{_hidden("csrf_token", csrf)}'
                f'{_hidden("producto", product)}{_hidden("flujo", key)}{back}'
                f'<label>Sumar un teléfono a la cohorte de {_esc(key)} '
                '<input type="tel" name="telefono" autocomplete="off" inputmode="tel" maxlength="32" required>'
                '</label><button type="submit">Sumar a la cohorte</button></form>')
    return out


def _render_actions(proposal: Mapping[str, Any], *, proposal_id: str, csrf: str, flows: list[Mapping[str, Any]],
                    done: Mapping[str, str]) -> str:
    status = proposal.get("effective_status") or proposal.get("status")
    validation = proposal.get("validation") if isinstance(proposal.get("validation"), Mapping) else {}
    preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
    simulation = preview.get("simulacion")
    built = isinstance(simulation, Mapping) and simulation.get("status") == "built"
    fingerprint = proposal.get("fingerprint")
    turns_off = only_turns_off(proposal.get("items"))
    parts = []
    if status in ("validada", "aprobada") and not built and not turns_off:
        parts.append('<div class="notice"><p>Sin simulación construida, la página no publica: la publica el operador '
                     "con <code>--sin-simulacion</code>.</p></div>")
    elif (status in ("validada", "aprobada") and isinstance(fingerprint, str)
          and done.get("hecho") != "flow_change_stale_base"):
        if turns_off:
            label = "Reintentar apagar el flujo" if status == "aprobada" else "Aprobar y apagar el flujo"
            help_text = ("Apaga el flujo: deja de mandar. Apagar solo saca envíos, así que se publica sin simulación.")
        else:
            label = "Reintentar publicar" if status == "aprobada" else "Aprobar y publicar"
            help_text = "Publica el cambio de esta propuesta."
        parts.append(f'<form method="post" action="{PREFIX}/p/{_esc(proposal_id)}/publish">'
                     f'{_hidden("csrf_token", csrf)}{_hidden("fingerprint", fingerprint)}'
                     f'<button type="submit" class="button primary">{label}</button>'
                     f'<p class="meta">{_esc(help_text)}</p></form>')
    elif status == "publicada":
        parts.append('<div class="notice notice--ok"><p>Esta propuesta ya está publicada.</p></div>')
    else:
        parts.append(f'<div class="notice"><p>Esta propuesta está {_esc(_STATUS.get(str(status), status))}: '
                     "no se publica.</p></div>")
    if status in ("borrador", "validada", "aprobada"):
        parts.append(f'<div class="divider"></div><form method="post" action="{PREFIX}/p/{_esc(proposal_id)}/discard">'
                     f'{_hidden("csrf_token", csrf)}<label>Motivo para descartar el pedido (una línea) '
                     f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" autocomplete="off" required>'
                     '</label><button type="submit">Descartar este pedido</button>'
                     f'<p class="meta">{_esc(DISCARD_HELP)}</p></form>')
    touched = {(item.get("producto"), item.get("flujo")) for item in proposal.get("items") or []
               if isinstance(item, Mapping)}
    forms = "".join(_flow_forms(flow, csrf, proposal_id) for flow in flows
                    if isinstance(flow, Mapping) and (flow.get("product_ref"), flow.get("flow_key")) in touched)
    if forms:
        parts.append(f'<div class="divider"></div><h3>Los flujos de esta propuesta</h3>{forms}')
    return "".join(parts)


# Lo que dice el boton de descartar (E-235, 2): descarta la propuesta, no el flujo.
DISCARD_HELP = ("Descarta este pedido, no el flujo: el flujo queda como está ahora, sin ningún cambio. "
                "Para frenar un flujo ya, usá «Pausar».")


def _render_second_step(proposal: Mapping[str, Any], *, proposal_id: str, csrf: str, confirm: str,
                        reasons: list[tuple[str, str]], others: list[Mapping[str, Any]]) -> str:
    validation = proposal.get("validation") if isinstance(proposal.get("validation"), Mapping) else {}
    preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
    in_flight = preview.get("en_curso") if isinstance(preview.get("en_curso"), Mapping) else {}
    count = in_flight.get("recorridos")
    yes = "Sí, aprobar y apagar el flujo" if only_turns_off(proposal.get("items")) else "Sí, aprobar y publicar"
    lines = "".join(f"<li><strong>{_esc(name)}</strong>: {_esc(SECOND_STEP_TEXT.get(reason, reason))}</li>"
                    for name, reason in reasons)
    warn = ""
    if others:
        warn = ('<div class="notice notice--warn"><p>Hay otra propuesta abierta de este flujo. Si publicás esta, la '
                "otra va a tener que validarse de nuevo.</p></div>")
    return ('<header class="page-header"><p class="meta">Segundo paso</p><h1>Confirmá antes de publicar</h1>'
            '<p class="meta">Este cambio llega a más gente que la cohorte de prueba o corta algo en curso: por eso '
            'se pide dos veces.</p></header><section class="confirm panel">'
            f'<div class="actions">{warn}<div class="notice notice--warn"><ul>{lines}</ul>'
            f"<p>Lo que está en curso: {_esc(count) + ' recorridos' if count is not None else 'sin contar'}.</p></div>"
            f'<form method="post" action="{PREFIX}/p/{_esc(proposal_id)}/publish">{_hidden("csrf_token", csrf)}'
            f'{_hidden("fingerprint", proposal.get("fingerprint"))}{_hidden("confirmar", confirm)}'
            f'<button type="submit" class="button primary">{yes}</button></form>'
            f'<p class="meta"><a href="{PREFIX}/p/{_esc(proposal_id)}">Volver sin publicar</a></p></div></section>')


def _render_index(store: Mapping[str, Any], open_rows: list[Mapping[str, Any]], *, csrf: str = "",
                  done: Mapping[str, str] | None = None) -> str:
    flows = [flow for flow in (store.get("flows") if isinstance(store.get("flows"), list) else [])
             if isinstance(flow, Mapping)]

    def name_of(flow: Mapping[str, Any]) -> str | None:
        versions = flow.get("versions") if isinstance(flow.get("versions"), Mapping) else {}
        active = versions.get(str(flow.get("active_version"))) if flow.get("active_version") is not None else None
        return active.get("nombre") if isinstance(active, Mapping) and isinstance(active.get("nombre"), str) else None

    rows = "".join(
        f'<div class="row"><div><div class="row-name">{_esc(name_of(flow) or _flow_name(flow.get("product_ref"), flow.get("flow_key")))}'
        f' {_chip(_ESTADO_FLUJO.get(str(flow.get("state")), flow.get("state")), _ESTADO_CHIP.get(str(flow.get("state"))))}</div>'
        f'<div class="row-sub">{_esc(flow.get("product_ref"))}/{_esc(flow.get("flow_key"))} · versión '
        f"{_esc(flow.get('active_version') if flow.get('active_version') is not None else '—')}</div></div>"
        f'<div class="row-forms">{_flow_forms(flow, csrf, None)}</div></div>'
        for flow in flows)
    proposals = "".join(
        f'<a class="row" href="{PREFIX}/p/{_esc(row.get("proposal_id"))}"><div><div class="row-name">'
        f"{_esc(', '.join(f'{flow.get('producto')}/{flow.get('flujo')}' for flow in row.get('flows') or []))}</div>"
        f'<div class="row-sub">vence {_esc(row.get("expires_at"))}</div></div>'
        f"<div>{_chip(_STATUS.get(str(row.get('status')), row.get('status')), _STATUS_CHIP.get(str(row.get('status'))))}"
        "</div></a>"
        for row in open_rows if isinstance(row, Mapping))
    return (_render_done(done or {})
            + '<header class="page-header"><p class="meta">Aprobación de flujos</p><h1>Flujos de la instancia</h1>'
            '<p class="meta">Los mensajes automáticos que manda el recuperador y lo que el agente propone cambiar.</p>'
            '</header><div class="stack"><section class="panel"><div class="panel-heading">'
            "<h2>Propuestas esperando el sí</h2><p>Abrí una para ver qué cambia y decidir.</p></div>"
            + (proposals if proposals else '<p class="empty">Ninguna.</p>')
            + '</section><section class="panel"><div class="panel-heading"><h2>Los flujos</h2>'
            "<p>Pausar frena los recorridos nuevos; los que están en curso esperan sin mandar.</p></div>"
            + (rows if rows else '<p class="empty">Esta instancia no tiene flujos.</p>') + "</section></div>")


# ---------------------------------------------------------------------------
# Las rutas.
# ---------------------------------------------------------------------------


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_origin(request: Request, origin: str) -> bool:
    sent = request.headers.get("origin")
    if sent is not None and sent != "null":
        return hmac.compare_digest(sent, origin)
    return (request.headers.get("sec-fetch-site") == "same-origin"
            and request.headers.get("host") == urlsplit(origin).netloc)


async def _bounded_form(request: Request, *, max_fields: int = 4) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ValueError("invalid_form_content_type")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > 4096):
        raise ValueError("form_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 4096:
            raise ValueError("form_too_large")
        chunks.append(chunk)
    body = b"".join(chunks)
    parsed = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True, strict_parsing=bool(body),
                      max_num_fields=max_fields)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("duplicate_form_field")
    return {key: values[0] for key, values in parsed.items()}


def _done_from_query(request: Request, config: FlowApprovalConfig, session_secret: str) -> dict[str, str]:
    """Lo que vuelve por la redireccion despues de escribir: codigos cerrados y numeros chicos, nada mas.

    Solo cuenta si viene firmado para esta sesion y esta pagina (``done_token``):
    un link armado a mano no dice «Publicada» ni esconde el boton.
    """
    raw = {key: value for key in _DONE_KEYS if (value := request.query_params.get(key)) is not None}
    if not raw:
        return {}
    sent = request.query_params.get("f") or ""
    if not (_HMAC.fullmatch(sent) and hmac.compare_digest(
            sent, config.done_token(session_secret, request.url.path, raw))):
        return {}
    out: dict[str, str] = {}
    code = request.query_params.get("hecho")
    if isinstance(code, str) and _CODE.fullmatch(code):
        out["hecho"] = code
    notice = request.query_params.get("aviso")
    if notice in _NOTICE:
        out["aviso"] = notice
    for key in ("miembros", "max"):
        value = request.query_params.get(key)
        if isinstance(value, str) and value.isdigit() and len(value) <= 3:
            out[key] = value
    return out


def _flows_of(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    flows = snapshot.get("flows")
    return [flow for flow in flows if isinstance(flow, Mapping)] if isinstance(flows, list) else []


def register_flow_approval_routes(app: FastAPI, *, config: FlowApprovalConfig, store: ApprovalStore,
                                  oidc: SlackIdTokenClient, chatwoot: Any = None, inbox_id: int | None = None,
                                  time_zone: str | None = None, producer: Any = None,
                                  environ: Mapping[str, str] | None = None,
                                  clock: Callable[[], datetime] | None = None) -> None:
    """Las rutas de la pagina. Solo se llama con el flag prendido.

    ``chatwoot`` e ``inbox_id`` dan el catalogo de plantillas para revalidar al
    publicar (sin ellos, publicar contesta ``template_catalog_unavailable``);
    ``time_zone`` es la de la instancia; ``producer``, el del conector de Slack
    (None: la pagina no avisa y lo dice); ``environ``, la identidad de la imagen
    que firma la revalidacion (``SETTER_VERSION`` y ``GIT_SHA``).
    """
    now = clock or (lambda: datetime.now(tz=UTC))
    instance = dict(config.instance)
    image_environ: Mapping[str, str] = environ if environ is not None else os.environ

    @app.middleware("http")
    async def harden_flow_approval(request: Request,
                                   call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not (request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/")):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - la pagina nunca muestra una traza
            logger.exception("flow_approval route=%s status=500", request.url.path.split("/")[2:3])
            response = _page("Aprobar cambios de flujos", "No se pudo completar la solicitud.", 500)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        return response

    async def _who(request: Request) -> tuple[str, str] | None:
        """El ID de Slack de una sesion viva y en la lista, y el secreto de la cookie; o None."""
        secret = request.cookies.get(SESSION_COOKIE)
        if not isinstance(secret, str) or not _BROWSER_SECRET.fullmatch(secret):
            return None
        try:
            session = await store.validate_session(instance, session_hash=_sha(secret))
        except SessionInvalid:
            return None
        user = session.get("slack_user_id")
        if (not isinstance(user, str) or user not in config.approvers
                or session.get("slack_team_id") != config.slack_team_id):
            # Sacarlo de la lista le corta el acceso en el proximo pedido.
            logger.info("flow_approval session=not_in_approvers")
            return None
        return user, secret

    def _logged_out(response: Response) -> Response:
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get(PREFIX + "/")
    async def index(request: Request) -> Response:
        who = await _who(request)
        if who is None:
            return _logged_out(_login(None))
        user, secret = who
        try:
            flows = await store.read_store(instance, session_hash=_sha(secret))
            open_rows = await store.read_open(instance, session_hash=_sha(secret), proposal_id=None)
        except SessionInvalid:
            return _logged_out(_login(None))
        except (StoreUnavailable, StoreRejected):
            logger.info("flow_approval route=index status=503")
            return _page("Aprobar cambios de flujos", "El almacén de flujos no contestó. Probá en un rato.", 503)
        rows = open_rows.get("proposals") if isinstance(open_rows.get("proposals"), list) else []
        csrf = config.csrf_token(secret)
        return HTMLResponse(_shell("Flujos", _render_index(flows, rows, csrf=csrf, done=_done_from_query(request, config, secret)),
                                   user=user, csrf=csrf))

    @app.get(PREFIX + "/p/{proposal_id}")
    async def proposal(proposal_id: str, request: Request) -> Response:
        if not _canonical_uuid(proposal_id):
            return _page("Propuesta", "No existe esa propuesta.", 404)
        who = await _who(request)
        if who is None:
            return _logged_out(_login(proposal_id))
        user, secret = who
        try:
            found = await store.get_proposal(instance, session_hash=_sha(secret), proposal_id=proposal_id)
            others = await store.read_open(instance, session_hash=_sha(secret), proposal_id=proposal_id)
            snapshot = await store.read_store(instance, session_hash=_sha(secret))
        except SessionInvalid:
            return _logged_out(_login(proposal_id))
        except StoreRejected as rejected:
            if rejected.code == "flow_change_not_found":
                return _page("Propuesta", "No existe esa propuesta en esta instancia.", 404)
            return _page("Propuesta", "No se pudo leer la propuesta.", 503)
        except StoreUnavailable:
            logger.info("flow_approval route=proposal status=503")
            return _page("Propuesta", "El almacén de flujos no contestó. Probá en un rato.", 503)
        rows = others.get("proposals") if isinstance(others.get("proposals"), list) else []
        logger.info("flow_approval route=proposal proposal=%s others=%d", proposal_id, len(rows))
        csrf = config.csrf_token(secret)
        done = _done_from_query(request, config, secret)
        actions = _render_actions(found, proposal_id=proposal_id, csrf=csrf, flows=_flows_of(snapshot), done=done)
        body = _render_proposal(found, rows, actions=actions, done=done, flows=_flows_of(snapshot))
        return HTMLResponse(_shell("Propuesta", body, user=user, csrf=csrf))

    @app.get(PREFIX + "/auth/slack/start")
    async def slack_start(request: Request) -> Response:
        target = request.query_params.get("p")
        if target is not None and not _canonical_uuid(target):
            return _page("Entrar", "El link no es válido.", 404)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        return_path = f"{PREFIX}/p/{target}" if target else f"{PREFIX}/"
        try:
            begun = await store.begin_oidc(instance, state_hash=_sha(state), nonce_hash=_sha(nonce),
                                           return_path=return_path,
                                           expires_at=now() + timedelta(minutes=STATE_MINUTES))
        except (StoreUnavailable, StoreRejected, SessionInvalid):
            logger.info("flow_approval route=start status=503")
            return _page("Entrar", "No se pudo empezar a entrar. Probá en un rato.", 503)
        if begun.get("ok") is not True:
            logger.info("flow_approval route=start code=%s", begun.get("reason_code"))
            return _page("Entrar", "Hay demasiados ingresos pendientes. Probá en unos minutos.", 429)
        response = RedirectResponse(oidc.authorization_url(state=state, nonce=nonce,
                                                           redirect_uri=config.redirect_uri), status_code=303)
        response.set_cookie(STATE_COOKIE, state, max_age=STATE_MINUTES * 60, secure=True, httponly=True,
                            samesite="lax", path="/")
        return response

    @app.get(CALLBACK_PATH)
    async def slack_callback(request: Request) -> Response:
        state = request.query_params.get("state")
        code = request.query_params.get("code")
        cookie_state = request.cookies.get(STATE_COOKIE)
        if (not isinstance(state, str) or not _BROWSER_SECRET.fullmatch(state) or not isinstance(cookie_state, str)
                or not hmac.compare_digest(state, cookie_state) or not code or len(code) > 512):
            return _page("Entrar", "No se pudo entrar. Referencia: OIDC-STATE.", 401)
        try:
            consumed = await store.consume_oidc(instance, state_hash=_sha(state))
        except SessionInvalid:
            return _page("Entrar", "El ingreso venció o ya se usó. Volvé a abrir el link.", 401)
        except (StoreUnavailable, StoreRejected):
            return _page("Entrar", "No se pudo entrar. Probá en un rato.", 503)
        try:
            identity = await oidc.authenticate(code=code, redirect_uri=config.redirect_uri)
        except SlackOidcError as error:
            logger.info("flow_approval route=callback oidc=%s", error.reference)
            return _page("Entrar", f"No se pudo entrar. Referencia: {error.reference}.", 401)
        nonce_hash = consumed.get("nonce_hash")
        if not isinstance(nonce_hash, str) or not hmac.compare_digest(_sha(identity.nonce), nonce_hash):
            return _page("Entrar", "No se pudo entrar. Referencia: OIDC-NONCE.", 401)
        if identity.team_id != config.slack_team_id:
            return _page("Entrar", "Esa cuenta de Slack es de otro workspace.", 403)
        if identity.user_id not in config.approvers:
            logger.info("flow_approval route=callback approver=no")
            return _page("Entrar", "No estás en la lista de aprobadores de esta instancia.", 403)
        session = secrets.token_urlsafe(32)
        try:
            created = await store.create_session(instance, state_hash=_sha(state), session_hash=_sha(session),
                                                 team_id=identity.team_id, user_id=identity.user_id,
                                                 expires_at=now() + timedelta(hours=SESSION_HOURS))
        except SessionInvalid:
            return _page("Entrar", "El ingreso venció o ya se usó. Volvé a abrir el link.", 401)
        except (StoreUnavailable, StoreRejected):
            return _page("Entrar", "No se pudo entrar. Probá en un rato.", 503)
        return_path = consumed.get("return_path")
        if (not isinstance(return_path, str) or not (return_path == f"{PREFIX}/" or (
                return_path.startswith(f"{PREFIX}/p/") and _canonical_uuid(return_path[len(PREFIX) + 3:])))):
            return_path = f"{PREFIX}/"
        logger.info("flow_approval route=callback session=created ok=%s", created.get("ok") is True)
        response = RedirectResponse(return_path, status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_HOURS * 3600, secure=True, httponly=True,
                            samesite="lax", path="/")
        return response

    @app.post(PREFIX + "/logout")
    async def logout(request: Request) -> Response:
        if not _same_origin(request, config.origin):
            return _page("Salir", "Origen no autorizado.", 403)
        secret = request.cookies.get(SESSION_COOKIE)
        if not isinstance(secret, str) or not _BROWSER_SECRET.fullmatch(secret):
            return _logged_out(RedirectResponse(f"{PREFIX}/", status_code=303))
        try:
            form = await _bounded_form(request)
        except (ValueError, UnicodeDecodeError):
            return _page("Salir", "Formulario inválido.", 422)
        if set(form) != {"csrf_token"} or not hmac.compare_digest(form["csrf_token"], config.csrf_token(secret)):
            return _page("Salir", "Formulario inválido.", 403)
        try:
            await store.revoke_session(instance, session_hash=_sha(secret))
        except (StoreUnavailable, StoreRejected, SessionInvalid):
            return _page("Salir", "No se pudo cerrar la sesión. Probá en un rato.", 503)
        return _logged_out(RedirectResponse(f"{PREFIX}/", status_code=303))

    # -- las escrituras (A′ PR 3b) ------------------------------------------------

    async def _write_form(request: Request, *, title: str, fields: set[str],
                          optional: frozenset[str] = frozenset()) -> tuple[str, str, dict[str, str]] | Response:
        """Origin, sesion viva y en la lista, formulario acotado y CSRF; o la respuesta que corta."""
        if not _same_origin(request, config.origin):
            return _page(title, "Origen no autorizado.", 403)
        who = await _who(request)
        if who is None:
            return _logged_out(_login(None))
        user, secret = who
        try:
            form = await _bounded_form(request, max_fields=len(fields) + len(optional))
        except (ValueError, UnicodeDecodeError):
            return _page(title, "Formulario inválido.", 422)
        if not fields <= set(form) <= fields | optional:
            return _page(title, "Formulario inválido.", 422)
        token = form["csrf_token"]
        if not _HMAC.fullmatch(token) or not hmac.compare_digest(token, config.csrf_token(secret)):
            return _page(title, "Formulario inválido.", 403)
        return user, secret, form

    def _back(secret: str, path: str, **query: str | None) -> RedirectResponse:
        kept = {key: value for key, value in query.items() if value is not None}
        if kept:
            kept["f"] = config.done_token(secret, path, kept)
        return RedirectResponse(path + (f"?{urlencode(kept)}" if kept else ""), status_code=303)

    def _window() -> int:
        return int(now().timestamp() // CONFIRM_WINDOW.total_seconds())

    def _closed(code: object) -> str:
        return code if isinstance(code, str) and _CODE.fullmatch(code) else "flow_change_unknown"

    async def _notify(commands: list[NotificationCommand]) -> str:
        """El aviso al canal de alertas, despues de que la base confirmo. Si no sale, no se deshace nada."""
        if producer is None:
            return "sin_conector"
        result = "enviado"
        for command in commands:
            try:
                await producer.admit(command)
            except Exception as error:  # noqa: BLE001 - el aviso no deshace lo publicado o pausado
                logger.warning("flow_approval notice=failed code=%s error=%s", command.event_code,
                               type(error).__name__)
                result = "no_salio"
        return result

    def _notice(event_code: str, what: str, flow_key: object, user: str, key: str) -> list[NotificationCommand]:
        if not isinstance(flow_key, str) or not _REF.fullmatch(flow_key):
            return []
        try:
            return [approval_notice(event_code=event_code, what=what, flow_key=flow_key, user_id=user, key=key,
                                    occurred_at=now())]
        except ValueError:
            logger.warning("flow_approval notice=invalid code=%s", event_code)
            return []

    async def _catalog() -> dict[str, Any] | None:
        if chatwoot is None or inbox_id is None:
            return None
        try:
            inbox = await chatwoot.get_inbox(inbox_id=inbox_id)
        except Exception:  # noqa: BLE001 - sin catalogo no se aprueba, y se dice
            logger.info("flow_approval route=publish catalog=unavailable")
            return None
        if not isinstance(inbox, Mapping) or not isinstance(inbox.get("message_templates"), list):
            return None
        return {"message_templates": inbox["message_templates"]}

    def _revalidate(found: Mapping[str, Any], proposal_id: str, snapshot: Mapping[str, Any],
                    catalog: Mapping[str, Any]) -> dict[str, Any]:
        """La validacion en fase ``publish`` con la clase de quien pidio, como el comando del operador.

        La simulacion no cuenta para publicar (aprobar registra la congelada en la
        vista previa): la revalidacion no la arma.
        """
        products = snapshot.get("products")
        if not isinstance(products, list) or not products or not all(isinstance(p, str) for p in products):
            raise StoreUnavailable()
        return validate_proposal({
            "simulation_unavailable": "publish_revalidation",
            "instance": {**instance, "products": sorted(products), "time_zone": time_zone},
            "store": snapshot,
            "proposal": {"id": proposal_id, "revision": found.get("revision"), "items": found.get("items"),
                         "draft_base": found.get("draft_base"), "purpose": found.get("purpose"),
                         "requested_by_kind": found.get("requested_by_kind")},
            "catalog": catalog,
        }, environ=image_environ)

    @app.post(PREFIX + "/p/{proposal_id}/publish")
    async def publish(proposal_id: str, request: Request) -> Response:
        """Aprobar y publicar (contrato 1.9.0, «Publicar desde la pagina»)."""
        if not _canonical_uuid(proposal_id):
            return _page("Propuesta", "No existe esa propuesta.", 404)
        checked = await _write_form(request, title="Publicar", fields={"csrf_token", "fingerprint"},
                                    optional=frozenset({"confirmar"}))
        if isinstance(checked, Response):
            return checked
        user, secret, form = checked
        page = f"{PREFIX}/p/{proposal_id}"
        session_hash = _sha(secret)
        try:
            # 1. Relee la propuesta del link y exige la huella que la pagina mostro.
            found = await store.get_proposal(instance, session_hash=session_hash, proposal_id=proposal_id)
            fingerprint = found.get("fingerprint")
            if not (isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(form["fingerprint"])
                    and hmac.compare_digest(form["fingerprint"], fingerprint)):
                logger.info("flow_approval route=publish proposal=%s result=fingerprint_changed", proposal_id)
                return _back(secret, page)
            status = found.get("effective_status") or found.get("status")
            if status == "publicada":
                return _back(secret, page, hecho="ya_publicada")
            if status not in ("validada", "aprobada"):
                return _back(secret, page, hecho="flow_change_proposal_expired" if status == "vencida"
                             else "flow_change_not_approved")
            validation = found.get("validation") if isinstance(found.get("validation"), Mapping) else {}
            preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
            stored = preview.get("simulacion")
            if not (isinstance(stored, Mapping) and stored.get("status") == "built") \
                    and not only_turns_off(found.get("items")):
                # Sin simulacion construida, la pagina no publica (decision 10 del 2026-10-27), salvo
                # una propuesta que solo apaga flujos, que solo saca envios (E-235, valor por defecto
                # que revisa Dan). Los items salen de la propuesta releida, congelados por su huella.
                return _back(secret, page, hecho="simulation_not_built")
            snapshot = await store.read_store(instance, session_hash=session_hash)
            # Un clic o dos pasos: lo decide el bridge, con el almacen de ahora.
            reasons = second_step_reasons(found.get("items"), snapshot.get("flows"))
            if reasons:
                window = _window()
                expected = config.confirm_token(secret, proposal_id, fingerprint, reasons, window)
                previous = config.confirm_token(secret, proposal_id, fingerprint, reasons, window - 1)
                given = form.get("confirmar", "")
                if not (_HMAC.fullmatch(given) and (hmac.compare_digest(given, expected)
                                                    or hmac.compare_digest(given, previous))):
                    others = await store.read_open(instance, session_hash=session_hash, proposal_id=proposal_id)
                    rows = others.get("proposals") if isinstance(others.get("proposals"), list) else []
                    csrf = config.csrf_token(secret)
                    logger.info("flow_approval route=publish proposal=%s result=second_step", proposal_id)
                    return HTMLResponse(_shell("Confirmar", _render_second_step(
                        found, proposal_id=proposal_id, csrf=csrf, confirm=expected, reasons=reasons, others=rows),
                        user=user, csrf=csrf))
            # 2. El catalogo de plantillas de la bandeja. Sin catalogo, no aprueba.
            catalog = await _catalog()
            if catalog is None:
                return _back(secret, page, hecho="template_catalog_unavailable")
            # 3. Valida de nuevo en fase publish, con la clase de quien pidio. Con bloqueos, no aprueba.
            try:
                checked_now = _revalidate(found, proposal_id, snapshot, catalog)
            except (OperatorInputError, StoreUnavailable):
                return _back(secret, page, hecho="validator_unavailable")
            validated_at = now()
            if checked_now.get("ok") is not True:
                return _back(secret, page, hecho="revalidation_blocked")
            if checked_now.get("catalog_sha256") is None:
                return _back(secret, page, hecho="template_catalog_unavailable")
            # 4. Aprueba con el ID de Slack de la sesion (lo pone la base).
            if status == "validada":
                approved = await store.approve(instance, session_hash=session_hash, proposal_id=proposal_id,
                                               fingerprint=fingerprint)
                if approved.get("ok") is not True:
                    logger.info("flow_approval route=publish proposal=%s result=%s", proposal_id,
                                _closed(approved.get("reason_code")))
                    return _back(secret, page, hecho=_closed(approved.get("reason_code")))
            # 5. Pasados 8 minutos, valida otra vez, con la propuesta y el almacen releidos.
            if now() - validated_at > REVALIDATE_AFTER:
                found = await store.get_proposal(instance, session_hash=session_hash, proposal_id=proposal_id)
                if not (isinstance(found.get("fingerprint"), str)
                        and hmac.compare_digest(found["fingerprint"], fingerprint)):
                    return _back(secret, page)
                snapshot = await store.read_store(instance, session_hash=session_hash)
                try:
                    checked_now = _revalidate(found, proposal_id, snapshot, catalog)
                except (OperatorInputError, StoreUnavailable):
                    return _back(secret, page, hecho="validator_unavailable")
                validated_at = now()
                if checked_now.get("ok") is not True or checked_now.get("catalog_sha256") is None:
                    return _back(secret, page, hecho="revalidation_blocked")
            # 6. Publica con esa revalidacion, la huella y la zona de la instancia.
            revalidation = {"catalog_sha256": checked_now["catalog_sha256"], "fingerprint": fingerprint,
                            "result": checked_now["result"], "validator": checked_now["validator"],
                            "validated_at": _utc(validated_at)}
            zoned = {**instance, "time_zone": time_zone} if time_zone else dict(instance)
            published = await store.publish(zoned, session_hash=session_hash, proposal_id=proposal_id,
                                             fingerprint=fingerprint, revalidation=revalidation)
        except SessionInvalid:
            return _logged_out(_login(proposal_id))
        except StoreRejected as rejected:
            if rejected.code == "flow_change_not_found":
                return _page("Propuesta", "No existe esa propuesta en esta instancia.", 404)
            return _back(secret, page, hecho=rejected.code)
        except StoreUnavailable:
            logger.info("flow_approval route=publish proposal=%s status=503", proposal_id)
            return _page("Publicar", "El almacén de flujos no contestó. Probá en un rato.", 503)
        if published.get("ok") is not True:
            code = _closed(published.get("reason_code"))
            logger.info("flow_approval route=publish proposal=%s result=%s", proposal_id, code)
            return _back(secret, page, hecho=code)
        if published.get("replayed") is True:
            return _back(secret, page, hecho="ya_publicada")
        what = ("publicada_cohorte" if not reasons
                else "publicada_todos" if any(reason == "llega_a_todos" for _, reason in reasons) else "publicada")
        commands = [command for item in found.get("items") or [] if isinstance(item, Mapping)
                    for command in _notice(NOTICE_PUBLISHED, what, item.get("flujo"), user, proposal_id)]
        notice = await _notify(commands)
        logger.info("flow_approval route=publish proposal=%s result=published notice=%s", proposal_id, notice)
        return _back(secret, page, hecho="publicada", aviso=notice)

    @app.post(PREFIX + "/p/{proposal_id}/discard")
    async def discard(proposal_id: str, request: Request) -> Response:
        if not _canonical_uuid(proposal_id):
            return _page("Propuesta", "No existe esa propuesta.", 404)
        checked = await _write_form(request, title="Descartar", fields={"csrf_token", "motivo"})
        if isinstance(checked, Response):
            return checked
        _, secret, form = checked
        page = f"{PREFIX}/p/{proposal_id}"
        reason = form["motivo"].strip()
        if not 1 <= len(reason) <= DISCARD_REASON_MAX or any(not character.isprintable() for character in reason):
            return _back(secret, page, hecho="flow_store_text_invalid")
        try:
            discarded = await store.discard(instance, session_hash=_sha(secret), proposal_id=proposal_id,
                                            reason=reason)
        except SessionInvalid:
            return _logged_out(_login(proposal_id))
        except StoreRejected as rejected:
            if rejected.code == "flow_change_not_found":
                return _page("Propuesta", "No existe esa propuesta en esta instancia.", 404)
            return _back(secret, page, hecho=rejected.code)
        except StoreUnavailable:
            return _page("Descartar", "El almacén de flujos no contestó. Probá en un rato.", 503)
        code = "descartada" if discarded.get("ok") is True else _closed(discarded.get("reason_code"))
        logger.info("flow_approval route=discard proposal=%s result=%s", proposal_id, code)
        return _back(secret, page, hecho=code)

    def _flow_target(form: Mapping[str, str]) -> tuple[str, str, str] | None:
        product, key, target = form["producto"], form["flujo"], form.get("p")
        if not (_REF.fullmatch(product) and _REF.fullmatch(key)):
            return None
        if target is not None and not _canonical_uuid(target):
            return None
        return product, key, f"{PREFIX}/p/{target}" if target else f"{PREFIX}/"

    @app.post(PREFIX + "/pause")
    async def pause(request: Request) -> Response:
        """Pausar: inmediato y sin propuesta, con la generacion que mostro la pagina."""
        checked = await _write_form(request, title="Pausar", fields={"csrf_token", "producto", "flujo", "generacion"},
                                    optional=frozenset({"p"}))
        if isinstance(checked, Response):
            return checked
        user, secret, form = checked
        target = _flow_target(form)
        generation = form["generacion"]
        if target is None or not generation.isdigit() or len(generation) > 18:
            return _page("Pausar", "Formulario inválido.", 422)
        product, key, page = target
        try:
            paused = await store.pause(instance, session_hash=_sha(secret), product_ref=product, flow_key=key,
                                       expected_generation=int(generation), reason=PAUSE_REASON)
        except SessionInvalid:
            return _logged_out(_login(None))
        except StoreRejected as rejected:
            return _back(secret, page, hecho=rejected.code)
        except StoreUnavailable:
            return _page("Pausar", "El almacén de flujos no contestó. Probá en un rato.", 503)
        if paused.get("ok") is not True:
            code = "generacion" if paused.get("reason_code") == "flow_control_generation_mismatch" \
                else _closed(paused.get("reason_code"))
            logger.info("flow_approval route=pause flow=%s result=%s", key, code)
            return _back(secret, page, hecho=code)
        if paused.get("changed") is not True:
            return _back(secret, page, hecho="ya_pausado")
        notice = await _notify(_notice(NOTICE_PAUSED, "pausado", key, user, f"{product}:{generation}"))
        logger.info("flow_approval route=pause flow=%s result=paused notice=%s", key, notice)
        return _back(secret, page, hecho="pausado", aviso=notice)

    @app.post(PREFIX + "/cohort")
    async def cohort(request: Request) -> Response:
        """Sumar un telefono a la cohorte. El telefono va a la base y a ningun otro lado."""
        checked = await _write_form(request, title="Cohorte", fields={"csrf_token", "producto", "flujo", "telefono"},
                                    optional=frozenset({"p"}))
        if isinstance(checked, Response):
            return checked
        _, secret, form = checked
        target = _flow_target(form)
        if target is None:
            return _page("Cohorte", "Formulario inválido.", 422)
        product, key, page = target
        phone = form["telefono"].strip()
        if not _PHONE.fullmatch(phone):
            return _back(secret, page, hecho="flow_cohort_phone_invalid")
        try:
            changed = await store.cohort_add(instance, session_hash=_sha(secret), product_ref=product, flow_key=key,
                                             phone=phone, reason=COHORT_REASON)
        except SessionInvalid:
            return _logged_out(_login(None))
        except StoreRejected as rejected:
            return _back(secret, page, hecho=rejected.code)
        except StoreUnavailable:
            return _page("Cohorte", "El almacén de flujos no contestó. Probá en un rato.", 503)
        finally:
            del phone
        if changed.get("ok") is not True:
            code = _closed(changed.get("reason_code"))
            logger.info("flow_approval route=cohort flow=%s result=%s", key, code)
            return _back(secret, page, hecho=code)
        members, maximum = changed.get("members"), changed.get("max_members")
        logger.info("flow_approval route=cohort flow=%s result=%s", key,
                    "added" if changed.get("changed") is True else "already")
        return _back(secret, page, hecho="cohorte" if changed.get("changed") is True else "cohorte_ya_estaba",
                     miembros=str(members) if isinstance(members, int) else None,
                     max=str(maximum) if isinstance(maximum, int) else None)
