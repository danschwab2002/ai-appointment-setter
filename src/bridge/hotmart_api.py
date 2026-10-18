"""Cliente minimo de la API de Hotmart para los cupones personales del carrito.

Lo usa ``bridge.recovery_coupons``: crea un cupon por caso y lo vuelve a leer
para saber si ya aplica. Solo tres llamadas: el token (client credentials), la
creacion del cupon y la lectura por codigo.

Ninguna credencial ni token sale de este modulo: los errores llevan un codigo
corto (``hotmart_http_400``, ``hotmart_timeout``) y nunca la URL ni el cuerpo,
porque la URL del token lleva el client_secret en la query string.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

HOTMART_TOKEN_URL = "https://api-sec-vlc.hotmart.com/security/oauth/token"
HOTMART_API_BASE_URL = "https://developers.hotmart.com"

CREDENTIAL_KEYS = ("HOTMART_CLIENT_ID", "HOTMART_CLIENT_SECRET", "HOTMART_BASIC")

# El token dura ~48 h (expires_in 172799 medido el 18/10); se renueva un rato
# antes para que una llamada nunca salga con un token a punto de vencer.
TOKEN_REFRESH_MARGIN_SECONDS = 600.0

# Hotmart corre las fechas del cupon +3 h: guarda los ms que recibe como hora
# de pared y los etiqueta America/Sao_Paulo (UTC-3). EMPIRICO, medido contra
# produccion el 18/10/2026; la doc oficial no lo dice.
# - Para que el cupon valga ya, el inicio va 4 h atras (las 3 del corrimiento
#   y una de margen).
# - Para que venza a la hora real T, el fin va 3 h antes de T.
HOTMART_COUPON_START_BACKDATE = timedelta(hours=4)
HOTMART_COUPON_WALL_CLOCK_SHIFT = timedelta(hours=3)

_PRODUCT_ID_RE = re.compile(r"[0-9]{1,20}")
_COUPON_CODE_RE = re.compile(r"[A-Z0-9]{1,25}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CREDENTIAL_QUERY_RE = re.compile(r"(client_secret|client_id)=[^&\s\"']*")


class _RedactCredentialQuery(logging.Filter):
    """httpx loguea en INFO la URL de cada pedido, y la del token lleva el
    client_secret en la query string: se tapa antes de que salga."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                _CREDENTIAL_QUERY_RE.sub(r"\1=<redacted>", str(arg))
                if "client_secret=" in str(arg) or "client_id=" in str(arg)
                else arg
                for arg in record.args
            )
        if isinstance(record.msg, str) and (
            "client_secret=" in record.msg or "client_id=" in record.msg
        ):
            record.msg = _CREDENTIAL_QUERY_RE.sub(r"\1=<redacted>", record.msg)
        return True


def _install_httpx_redaction() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactCredentialQuery) for f in httpx_logger.filters):
        httpx_logger.addFilter(_RedactCredentialQuery())


_install_httpx_redaction()


class HotmartApiError(RuntimeError):
    """Falla de la API de Hotmart. ``code`` es corto y sin datos."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class HotmartTransientError(HotmartApiError):
    """Timeout, error de red, 5xx o 429: reintentar puede andar."""


class HotmartPermanentError(HotmartApiError):
    """4xx: el mismo pedido no va a pasar."""


class HotmartCredentialsError(ValueError):
    """El archivo de credenciales falta o esta incompleto (sin valores)."""


@dataclass(frozen=True)
class HotmartApiCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    basic: str = field(repr=False)

    def __repr__(self) -> str:
        return "HotmartApiCredentials(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class HotmartCoupon:
    coupon_id: int | None
    code: str
    status: str | None
    active: bool | None
    start_ms: int | None


def load_hotmart_api_credentials(path: Path | str) -> HotmartApiCredentials:
    """Lee el archivo ``KEY=VALUE`` del servidor. Los errores nombran claves."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HotmartCredentialsError(
            "hotmart_api_credentials_file_unreadable"
        ) from None
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        if key in CREDENTIAL_KEYS:
            values[key] = value
    missing = [key for key in CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        raise HotmartCredentialsError(
            "hotmart_api_credentials_missing: " + ", ".join(missing)
        )
    return HotmartApiCredentials(
        client_id=values["HOTMART_CLIENT_ID"],
        client_secret=values["HOTMART_CLIENT_SECRET"],
        basic=values["HOTMART_BASIC"],
    )


def _epoch_ms(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def hotmart_coupon_window(
    now: datetime, real_expires_at: datetime
) -> tuple[int, int]:
    """Los ms que se le mandan a Hotmart para que el cupon valga ya y venza en T.

    Compensa el corrimiento de +3 h medido el 18/10 (ver las constantes).
    Falla si la ventana queda vacia: el vencimiento real ya paso.
    """
    if now.tzinfo is None or real_expires_at.tzinfo is None:
        raise ValueError("hotmart_coupon_window_requires_aware_datetimes")
    start_ms = _epoch_ms(now - HOTMART_COUPON_START_BACKDATE)
    end_ms = _epoch_ms(real_expires_at - HOTMART_COUPON_WALL_CLOCK_SHIFT)
    if end_ms <= start_ms:
        raise ValueError("hotmart_coupon_window_empty")
    return start_ms, end_ms


def _basic_header(value: str) -> str:
    stripped = value.strip()
    if stripped[:6].lower() == "basic ":
        stripped = stripped[6:].strip()
    return f"Basic {stripped}"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _validate_target(product_id: str, code: str) -> None:
    if not isinstance(product_id, str) or _PRODUCT_ID_RE.fullmatch(product_id) is None:
        raise HotmartPermanentError("hotmart_product_id_invalid")
    if not isinstance(code, str) or _COUPON_CODE_RE.fullmatch(code) is None:
        raise HotmartPermanentError("hotmart_coupon_code_invalid")


class HotmartApiClient:
    """Cliente async con el token cacheado hasta un rato antes de vencer."""

    def __init__(
        self,
        *,
        credentials: HotmartApiCredentials,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        token_url: str = HOTMART_TOKEN_URL,
        api_base_url: str = HOTMART_API_BASE_URL,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._token_url = token_url
        self._api_base_url = api_base_url.rstrip("/")
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "HotmartApiClient(<redacted>)"

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        operation: str,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_seconds
            ) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
        except httpx.TimeoutException:
            # from None: la excepcion de httpx puede traer la URL (y en el
            # token, la URL lleva el secreto).
            raise HotmartTransientError(f"{operation}_timeout") from None
        except httpx.HTTPError:
            raise HotmartTransientError(f"{operation}_transport_error") from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise HotmartTransientError(
                f"{operation}_http_{status}", status_code=status
            )
        return response

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_valid_until = 0.0

    async def get_token(self) -> str:
        if self._token is not None and self._monotonic() < self._token_valid_until:
            return self._token
        async with self._token_lock:
            if self._token is not None and self._monotonic() < self._token_valid_until:
                return self._token
            response = await self._send(
                "POST",
                self._token_url,
                headers={"Authorization": _basic_header(self._credentials.basic)},
                params={
                    "grant_type": "client_credentials",
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                },
                operation="hotmart_token",
            )
            if response.status_code != 200:
                raise HotmartPermanentError(
                    f"hotmart_token_http_{response.status_code}",
                    status_code=response.status_code,
                )
            try:
                body = response.json()
            except ValueError:
                raise HotmartTransientError("hotmart_token_invalid_json") from None
            token = body.get("access_token") if isinstance(body, dict) else None
            expires_in = body.get("expires_in") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise HotmartTransientError("hotmart_token_missing")
            if (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))
                or expires_in <= 0
            ):
                raise HotmartTransientError("hotmart_token_expiry_invalid")
            self._token = token
            self._token_valid_until = self._monotonic() + max(
                float(expires_in) - TOKEN_REFRESH_MARGIN_SECONDS,
                float(expires_in) / 2,
            )
            return token

    async def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        operation: str,
    ) -> httpx.Response:
        token = await self.get_token()
        response = await self._send(
            method,
            f"{self._api_base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json_body=json_body,
            operation=operation,
        )
        if response.status_code == 401:
            # El token cacheado dejo de valer: el proximo intento pide otro.
            self._invalidate_token()
            raise HotmartTransientError(f"{operation}_http_401", status_code=401)
        return response

    async def create_coupon(
        self,
        product_id: str,
        code: str,
        discount: float,
        start_ms: int,
        end_ms: int,
    ) -> HotmartCoupon:
        _validate_target(product_id, code)
        response = await self._api(
            "POST",
            f"/products/api/v1/product/{product_id}/coupon",
            json_body={
                "code": code,
                "discount": float(discount),
                "start_date": int(start_ms),
                "end_date": int(end_ms),
            },
            operation="hotmart_coupon_create",
        )
        if not 200 <= response.status_code < 300:
            raise HotmartPermanentError(
                f"hotmart_coupon_create_http_{response.status_code}",
                status_code=response.status_code,
            )
        # La doc dice que responde {}; en produccion (18/10) vino el cupon.
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        active = body.get("active")
        status = body.get("status")
        return HotmartCoupon(
            coupon_id=_optional_int(body.get("id")),
            code=code,
            status=status if isinstance(status, str) else None,
            active=active if isinstance(active, bool) else None,
            start_ms=_optional_int(body.get("startDate")) or int(start_ms),
        )

    async def find_coupon(self, product_id: str, code: str) -> HotmartCoupon | None:
        _validate_target(product_id, code)
        response = await self._api(
            "GET",
            f"/products/api/v1/coupon/product/{product_id}",
            params={"code": code},
            operation="hotmart_coupon_find",
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HotmartPermanentError(
                f"hotmart_coupon_find_http_{response.status_code}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise HotmartTransientError("hotmart_coupon_find_invalid_json") from None
        items = body.get("items") if isinstance(body, dict) else None
        if items is None:
            return None
        if not isinstance(items, list):
            raise HotmartTransientError("hotmart_coupon_find_invalid_shape")
        for item in items:
            # El filtro por codigo podria no ser exacto: se exige el codigo igual.
            if not isinstance(item, dict) or item.get("coupon_code") != code:
                continue
            status = item.get("status")
            active = item.get("active")
            return HotmartCoupon(
                coupon_id=_optional_int(item.get("id")),
                code=code,
                status=status if isinstance(status, str) else None,
                active=active if isinstance(active, bool) else None,
                start_ms=_optional_int(item.get("start_date")),
            )
        return None
