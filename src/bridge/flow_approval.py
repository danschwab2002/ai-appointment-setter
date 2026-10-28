"""La pagina de aprobacion de un cambio de flujo (flow-change-cycle-v1 1.9.0, A′ PR 3a).

El Automation Expert deja en su DM un boton de URL a ``/flow-approval/p/<id>``
(A′ PR 1). Esta pagina pide entrar con «Sign in with Slack» y muestra la
propuesta: la vista previa guardada al validar, el para que como texto no
verificado, a quien llega, lo que esta en curso, la simulacion, las
advertencias, como se vuelve atras y si hay otra propuesta abierta del mismo
flujo. **Esta version no escribe:** aprobar, publicar, pausar, descartar y la
cohorte llegan con el PR 3b. Mientras tanto, el si lo da el operador.

Las rutas, todas bajo ``/flow-approval/`` y solo con ``FLOW_APPROVAL_ENABLED``
(apagado, no existen y dan 404):

* ``GET /flow-approval/``: la lista de flujos de la instancia y las propuestas
  validadas o aprobadas sin vencer.
* ``GET /flow-approval/p/{proposal_id}``: una propuesta.
* ``GET /flow-approval/auth/slack/start?p=<proposal_id>``: el ``state`` y el
  ``nonce`` (la base guarda sus sha256, 10 minutos) y la vuelta al OIDC de
  Slack con el scope ``openid``.
* ``GET /flow-approval/auth/slack/callback``: consume el ``state`` una vez,
  cambia el ``code`` por el token y valida el ``id_token`` (issuer, audiencia,
  vencimiento, ``nonce``, workspace y ``sub`` igual al ``user_id``). Recien
  ahi mira la lista de aprobadores; quien no esta no tiene sesion.
* ``POST /flow-approval/logout``: revoca la sesion (CSRF y Origin).

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
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

__all__ = [
    "DB_ROLE",
    "PREFIX",
    "ApprovalStore",
    "FlowApprovalConfig",
    "SessionInvalid",
    "SlackIdTokenClient",
    "SlackOidcError",
    "StoreUnavailable",
    "check_settings",
    "register_flow_approval_routes",
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


def _shell(title: str, body: str, *, user: str | None = None, csrf: str | None = None) -> str:
    logout = ""
    if user is not None and csrf is not None:
        logout = (f'<form method="post" action="{PREFIX}/logout" class="who"><span>Entraste como '
                  f'<code>{_esc(user)}</code></span><input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
                  '<button type="submit">Salir</button></form>')
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>
:root{{color-scheme:light dark;--ink:#17221c;--muted:#5b6b62;--paper:#fbfaf6;--line:#d8ddd8;--accent:#176b4b;--warn:#8a5a1f;--soft:#eef4ef}}
@media (prefers-color-scheme:dark){{:root{{--ink:#ecefed;--muted:#a3b0a8;--paper:#121614;--line:#2c3430;--accent:#4fbf8f;--warn:#e0b070;--soft:#1b221e}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}}
main{{width:min(860px,calc(100% - 32px));margin:24px auto 64px}}h1{{font-size:1.6rem;margin:0 0 6px}}h2{{font-size:1.1rem;margin:24px 0 8px}}
.muted{{color:var(--muted);font-size:.92rem}}.card{{border:1px solid var(--line);background:var(--soft);padding:14px 16px;margin:12px 0;overflow-wrap:anywhere}}
.warn{{border-color:var(--warn)}}.unverified{{white-space:pre-wrap;border-left:3px solid var(--warn);padding:8px 12px;background:transparent}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{border-top:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}}pre{{white-space:pre-wrap;margin:0}}
.who{{display:flex;gap:12px;align-items:center;justify-content:flex-end;font-size:.9rem;margin-bottom:12px;flex-wrap:wrap}}
button,.button{{min-height:40px;border:1px solid var(--ink);background:transparent;color:var(--ink);padding:8px 14px;font:600 .95rem inherit;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}}
.button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
@media(max-width:560px){{main{{margin-top:12px}}td,th{{padding:4px}}}}
</style></head><body><main>{logout}{body}</main></body></html>'''


def _page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(_shell(title, f"<h1>{_esc(title)}</h1><p>{_esc(message)}</p>"), status_code=status)


def _login(return_to: str | None) -> HTMLResponse:
    query = f"?{urlencode({'p': return_to})}" if return_to else ""
    body = ("<h1>Aprobar cambios de flujos</h1><p>Para ver la propuesta, entrá con tu cuenta de Slack.</p>"
            f'<p><a class="button primary" href="{PREFIX}/auth/slack/start{_esc(query)}">Entrar con Slack</a></p>')
    return HTMLResponse(_shell("Entrar", body), status_code=401)


_STATUS = {
    "borrador": "borrador (todavía no validada)", "validada": "validada, esperando el sí", "aprobada": "aprobada, sin publicar",
    "publicada": "publicada", "descartada": "descartada", "vencida": "vencida",
}
_A_QUIEN = {"cohorte": "solo la cohorte de prueba", None: "lo que ya alcanza cada flujo"}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _render_change(change: Mapping[str, Any]) -> str:
    head = (f"<p><strong>{_esc(change.get('producto'))}/{_esc(change.get('flujo'))}</strong> · "
            f"{_esc(change.get('tipo'))}")
    if change.get("base_version") is not None:
        head += f" · sobre la versión {_esc(change.get('base_version'))}"
    if change.get("nace_en"):
        head += f" · nace en {_esc(change.get('nace_en'))}"
    head += "</p>"
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
        head += f"<table><thead><tr><th>Campo</th><th>Antes</th><th>Después</th></tr></thead><tbody>{body}</tbody></table>"
    elif change.get("diff_count"):
        head += f"<p class=\"muted\">{_esc(change.get('diff_count'))} cambios, demasiado largos para mostrar.</p>"
    return f'<div class="card">{head}</div>'


def _render_simulation(simulation: object) -> str:
    if not isinstance(simulation, Mapping):
        return ("<p>Sin simulación construida. La página no publica sin simulación: la publica el operador con "
                "<code>--sin-simulacion</code>.</p>")
    if simulation.get("status") != "built":
        return (f"<p>La simulación no se pudo armar (<code>{_esc(simulation.get('reason_code'))}</code>). La página "
                "no publica sin simulación: la publica el operador con <code>--sin-simulacion</code>.</p>")
    summary = {key: value for key, value in simulation.items() if key not in ("status", "summary_sha256")}
    return ("<p>Estimación con los eventos reales de los últimos 7 días: no sabe qué habría contestado alguien a "
            f"un mensaje que no salió.</p><pre>{_esc(_json_text(summary))}</pre>")


def _render_proposal(proposal: Mapping[str, Any], others: list[Mapping[str, Any]]) -> str:
    status = proposal.get("effective_status") or proposal.get("status")
    validation = proposal.get("validation") if isinstance(proposal.get("validation"), Mapping) else {}
    preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
    parts = [f"<h1>Propuesta de cambio de flujos</h1><p class=\"muted\">{_esc(proposal.get('proposal_id'))} · "
             f"pedida por {_esc(proposal.get('requested_by_kind'))} · {_esc(_STATUS.get(str(status), status))}</p>"]
    if others:
        listed = "".join(
            f"<li>{_esc(', '.join(f'{flow.get('producto')}/{flow.get('flujo')}' for flow in other.get('flows') or []))}"
            f" · {_esc(_STATUS.get(str(other.get('status')), other.get('status')))}"
            f" · validada {_esc(other.get('validated_at') or 'sin validar')}</li>"
            for other in others)
        parts.append('<div class="card warn"><p><strong>Hay otra propuesta abierta de este flujo.</strong> Si '
                     f"publicás esta, la otra va a tener que validarse de nuevo.</p><ul>{listed}</ul></div>")
    if status == "vencida":
        parts.append('<div class="card warn"><p>Esta propuesta venció: pasaron 24 horas desde que el agente la '
                     "validó. Para seguir, pedile al agente que la valide de nuevo y te deja otro botón.</p></div>")
    purpose = preview.get("para_que", proposal.get("purpose"))
    parts.append("<h2>Para qué</h2><p class=\"muted\">Texto de quien propuso, sin verificar.</p>"
                 f'<div class="card unverified">{_esc(purpose)}</div>')
    parts.append(f"<h2>A quién llega</h2><p>{_esc(_A_QUIEN.get(preview.get('a_quien'), preview.get('a_quien')))}</p>")
    changes = preview.get("cambios")
    parts.append("<h2>Qué cambia</h2>")
    if isinstance(changes, list) and changes:
        parts.extend(_render_change(change) for change in changes if isinstance(change, Mapping))
    else:
        parts.append("<p class=\"muted\">La propuesta no tiene vista previa guardada.</p>")
    in_flight = preview.get("en_curso")
    if isinstance(in_flight, Mapping):
        count = in_flight.get("recorridos")
        parts.append(f"<h2>Lo que está en curso</h2><p>Política: {_esc(in_flight.get('politica'))}"
                     + (f" · {_esc(count)} recorridos" if count is not None else " · sin contar") + "</p>")
    parts.append("<h2>Simulación</h2>" + _render_simulation(preview.get("simulacion")))
    warnings = preview.get("advertencias")
    if isinstance(warnings, list) and warnings:
        parts.append("<h2>Advertencias</h2><ul>" + "".join(
            f"<li><code>{_esc(warning.get('code'))}</code> {_esc(warning.get('path') or '')}</li>"
            for warning in warnings if isinstance(warning, Mapping)) + "</ul>")
    back = preview.get("volver_atras")
    if isinstance(back, list) and back:
        lines = "".join(
            f"<li>{_esc(row.get('producto'))}/{_esc(row.get('flujo'))}: "
            + (f"vuelve a la versión {_esc(row.get('a_version'))}" if row.get("a_version") is not None
               else "es nuevo: se pausa o se apaga") + "</li>"
            for row in back if isinstance(row, Mapping))
        parts.append("<h2>Cómo se vuelve atrás</h2><p>Se pide en el DM del agente («volvé a la versión "
                     f"anterior») y llega con su propio botón.</p><ul>{lines}</ul>")
    parts.append('<h2>El sí</h2><div class="card"><p>Esta versión de la página solo muestra. Aprobar y publicar '
                 "llega con la próxima versión del bridge; mientras tanto, el sí lo da el operador.</p></div>")
    parts.append(f'<p><a href="{PREFIX}/">Ver todos los flujos</a></p>')
    return "".join(parts)


def _render_index(store: Mapping[str, Any], open_rows: list[Mapping[str, Any]]) -> str:
    flows = store.get("flows") if isinstance(store.get("flows"), list) else []
    rows = "".join(
        f"<tr><td>{_esc(flow.get('product_ref'))}/{_esc(flow.get('flow_key'))}</td><td>{_esc(flow.get('state'))}</td>"
        f"<td>{_esc(flow.get('active_version') if flow.get('active_version') is not None else '—')}</td></tr>"
        for flow in flows if isinstance(flow, Mapping))
    proposals = "".join(
        f'<li><a href="{PREFIX}/p/{_esc(row.get("proposal_id"))}">'
        f"{_esc(', '.join(f'{flow.get('producto')}/{flow.get('flujo')}' for flow in row.get('flows') or []))}</a>"
        f" · {_esc(_STATUS.get(str(row.get('status')), row.get('status')))} · vence {_esc(row.get('expires_at'))}</li>"
        for row in open_rows if isinstance(row, Mapping))
    return ("<h1>Flujos de la instancia</h1><table><thead><tr><th>Flujo</th><th>Estado</th><th>Versión</th></tr>"
            f"</thead><tbody>{rows}</tbody></table><h2>Propuestas esperando el sí</h2>"
            + (f"<ul>{proposals}</ul>" if proposals else "<p class=\"muted\">Ninguna.</p>"))


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


async def _bounded_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ValueError("invalid_form_content_type")
    body = await request.body()
    if len(body) > 4096:
        raise ValueError("form_too_large")
    parsed = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True, strict_parsing=bool(body),
                      max_num_fields=4)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("duplicate_form_field")
    return {key: values[0] for key, values in parsed.items()}


def register_flow_approval_routes(app: FastAPI, *, config: FlowApprovalConfig, store: ApprovalStore,
                                  oidc: SlackIdTokenClient,
                                  clock: Callable[[], datetime] | None = None) -> None:
    """Las rutas de la pagina (solo lectura en esta version). Solo se llama con el flag prendido."""
    now = clock or (lambda: datetime.now(tz=UTC))
    instance = dict(config.instance)

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
        return HTMLResponse(_shell("Flujos", _render_index(flows, rows), user=user, csrf=config.csrf_token(secret)))

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
        return HTMLResponse(_shell("Propuesta", _render_proposal(found, rows), user=user,
                                   csrf=config.csrf_token(secret)))

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
