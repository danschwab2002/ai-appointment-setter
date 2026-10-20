"""El cupon personal del carrito: cliente de Hotmart, worker y arranque.

Todo con transporte simulado: ningun test habla con Hotmart ni con Supabase.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.hotmart_api import (
    HotmartApiClient,
    HotmartApiCredentials,
    HotmartCredentialsError,
    hotmart_coupon_window,
    load_hotmart_api_credentials,
)
from bridge.recovery_coupons import RecoveryCouponWorker
from bridge.supabase import (
    RecoveryCouponClaim,
    RecoveryCouponSettlement,
    SupabaseClient,
    SupabasePermanentError,
)

CLIENT_ID = "client-id-not-for-logs"
CLIENT_SECRET = "client-secret-not-for-logs"
BASIC = "YmFzaWMtbm90LWZvci1sb2dz"
ACCESS_TOKEN = "access-token-not-for-logs"
PRODUCT_ID = "6712345"
CODE = "ATT1ABCDEF123456"
COUPON_ID = "11111111-1111-4111-8111-111111111111"
CASE_ID = "22222222-2222-4222-8222-222222222222"
T0 = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 10, 19, 15, 15, tzinfo=UTC)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# La respuesta real de GET /products/api/v1/coupon/product/{id}?code=..., capturada
# en ATT1 el 2026-10-20 sobre un cupon del carrito (el codigo, tapado). Sin la
# clave `active`: el worker la exigia en `true` y ningun cupon pasaba a `valid`.
FIND_FIXTURE = Path(__file__).parent / "fixtures" / "hotmart" / "coupon_find_att1_2026-10-20.json"


def _find_fixture_item() -> dict[str, Any]:
    return json.loads(FIND_FIXTURE.read_text())["items"][0]


def _credentials(basic: str = BASIC) -> HotmartApiCredentials:
    return HotmartApiCredentials(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, basic=basic
    )


class FakeHotmart:
    """Simula los tres endpoints de Hotmart que usa el cliente."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.coupons: dict[str, dict[str, Any]] = {}
        self.create_failures: list[object] = []
        self.find_status = "valid"
        # None: como Hotmart de verdad, sin la clave `active` (FIND_FIXTURE).
        self.find_active: bool | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/security/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 172799},
            )
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        if request.method == "POST" and path.endswith("/coupon"):
            if self.create_failures:
                failure = self.create_failures.pop(0)
                if isinstance(failure, Exception):
                    # El pedido llego: Hotmart lo crea aunque el cliente no
                    # vea la respuesta.
                    body = json.loads(request.content)
                    self.coupons[body["code"]] = body
                    raise failure
                return httpx.Response(int(failure), json={"error": "bad"})
            body = json.loads(request.content)
            self.coupons[body["code"]] = body
            return httpx.Response(
                200,
                json={
                    "id": 987,
                    "couponCode": body["code"],
                    "startDate": body["start_date"],
                    "endDate": body["end_date"],
                    "discount": body["discount"],
                    "status": "soon",
                    "active": True,
                },
            )
        if request.method == "GET" and "/coupon/product/" in path:
            code = request.url.params["code"]
            items = []
            if code in self.coupons:
                item = dict(_find_fixture_item())
                item.update(
                    id=987,
                    coupon_code=code,
                    status=self.find_status,
                    start_date=self.coupons[code]["start_date"],
                )
                if self.find_active is not None:
                    item["active"] = self.find_active
                items.append(item)
            return httpx.Response(200, json={"items": items, "page_info": {}})
        raise AssertionError(f"unexpected request {request.method} {path}")

    def count(self, method: str, suffix: str) -> int:
        return sum(
            1
            for request in self.requests
            if request.method == method and request.url.path.endswith(suffix)
        )

    def client(self, **kwargs: Any) -> HotmartApiClient:
        return HotmartApiClient(
            credentials=kwargs.pop("credentials", _credentials()),
            transport=httpx.MockTransport(self.handler),
            **kwargs,
        )


class FakeCouponStore:
    """La tabla recovery_coupons y sus dos RPC, con las transiciones de la base."""

    _ALLOWED = {
        "reserved": {"request_started", "created", "create_unknown", "create_failed"},
        "create_unknown": {"request_started", "created", "create_unknown", "create_failed"},
        "request_started": {"created", "create_unknown", "create_failed"},
        "created": {"valid", "created", "create_failed"},
    }

    def __init__(self) -> None:
        self.row: dict[str, Any] = {
            "status": "reserved",
            "next_attempt_at": None,
            "attempt_count": 0,
            "hotmart_start_ms": None,
            "hotmart_end_ms": None,
            "hotmart_coupon_id": None,
            "failure_code": None,
        }
        self.settles: list[dict[str, Any]] = []
        self.claim_calls: list[dict[str, Any]] = []

    async def claim_due_recovery_coupons(self, **kwargs: Any) -> list[RecoveryCouponClaim]:
        self.claim_calls.append(kwargs)
        now = datetime.fromisoformat(kwargs["now"])
        row = self.row
        if row["status"] not in {"reserved", "create_unknown", "created"}:
            return []
        if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
            return []
        row["attempt_count"] += 1
        return [
            RecoveryCouponClaim(
                coupon_id=COUPON_ID,
                recovery_case_id=CASE_ID,
                code=CODE,
                status=row["status"],
                external_product_id=PRODUCT_ID,
                expires_at=EXPIRES_AT,
                attempt_count=row["attempt_count"],
            )
        ]

    async def settle_recovery_coupon(self, **kwargs: Any) -> RecoveryCouponSettlement:
        self.settles.append(kwargs)
        row = self.row
        status = kwargs["status"]
        assert status in self._ALLOWED[row["status"]], (row["status"], status)
        for key, column in (
            ("hotmart_start_ms", "hotmart_start_ms"),
            ("hotmart_end_ms", "hotmart_end_ms"),
            ("hotmart_coupon_id", "hotmart_coupon_id"),
        ):
            if kwargs.get(key) is not None:
                row[column] = kwargs[key]
        if status in {"created", "valid"}:
            assert row["hotmart_start_ms"] is not None
            assert row["hotmart_end_ms"] > row["hotmart_start_ms"]
        row["status"] = status
        row["failure_code"] = kwargs.get("failure_code")
        next_attempt = kwargs.get("next_attempt_at")
        row["next_attempt_at"] = (
            datetime.fromisoformat(next_attempt) if next_attempt else None
        )
        return RecoveryCouponSettlement(coupon_id=COUPON_ID, status=status, applied=True)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _worker(
    store: FakeCouponStore, hotmart: HotmartApiClient, clock: Clock
) -> RecoveryCouponWorker:
    return RecoveryCouponWorker(
        supabase=store,  # type: ignore[arg-type]
        hotmart=hotmart,
        code_prefix="ATT1",
        worker_id="recovery-coupon:test",
        clock=clock,
    )


# ── La ventana (+3 h / -4 h) ───────────────────────────────────────


def test_window_compensates_the_measured_hotmart_shift() -> None:
    start_ms, end_ms = hotmart_coupon_window(T0, EXPIRES_AT)

    assert start_ms == _ms(datetime(2026, 10, 18, 11, 0, tzinfo=UTC))
    assert end_ms == _ms(datetime(2026, 10, 19, 12, 15, tzinfo=UTC))


def test_window_rejects_an_expiry_already_past_and_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="hotmart_coupon_window_empty"):
        hotmart_coupon_window(T0, T0 - timedelta(hours=1))
    with pytest.raises(ValueError, match="aware"):
        hotmart_coupon_window(T0.replace(tzinfo=None), EXPIRES_AT)


# ── El cliente ─────────────────────────────────────────────────────


def test_token_is_cached_across_two_creations_and_refreshed_after_expiry() -> None:
    hotmart = FakeHotmart()
    ticks = [0.0]
    client = hotmart.client(monotonic=lambda: ticks[0])

    async def scenario() -> None:
        await client.create_coupon(PRODUCT_ID, "ATT1AAAA00000001", 0.1, 1, 2)
        await client.create_coupon(PRODUCT_ID, "ATT1AAAA00000002", 0.1, 1, 2)
        assert hotmart.count("POST", "/oauth/token") == 1
        ticks[0] = 172799.0
        await client.create_coupon(PRODUCT_ID, "ATT1AAAA00000003", 0.1, 1, 2)

    asyncio.run(scenario())

    assert hotmart.count("POST", "/oauth/token") == 2
    token_request = hotmart.requests[0]
    assert token_request.url.params["grant_type"] == "client_credentials"
    assert token_request.url.params["client_id"] == CLIENT_ID
    create = json.loads(hotmart.requests[1].content)
    assert create == {
        "code": "ATT1AAAA00000001",
        "discount": 0.1,
        "start_date": 1,
        "end_date": 2,
    }


@pytest.mark.parametrize("stored", [BASIC, f"Basic {BASIC}", f"basic  {BASIC} "])
def test_basic_header_accepts_the_value_with_or_without_prefix(stored: str) -> None:
    hotmart = FakeHotmart()
    client = hotmart.client(credentials=_credentials(basic=stored))

    asyncio.run(client.get_token())

    assert hotmart.requests[0].headers["Authorization"] == f"Basic {BASIC}"


def test_create_tolerates_the_documented_empty_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 100})
        return httpx.Response(200, json={})

    client = HotmartApiClient(
        credentials=_credentials(), transport=httpx.MockTransport(handler)
    )
    coupon = asyncio.run(client.create_coupon(PRODUCT_ID, CODE, 0.1, 10, 20))

    assert coupon.coupon_id is None
    assert coupon.start_ms == 10


def test_find_requires_the_exact_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 100})
        return httpx.Response(
            200,
            json={"items": [{"id": 1, "coupon_code": CODE + "X", "status": "valid"}]},
        )

    client = HotmartApiClient(
        credentials=_credentials(), transport=httpx.MockTransport(handler)
    )

    assert asyncio.run(client.find_coupon(PRODUCT_ID, CODE)) is None


def test_find_reads_the_captured_response_without_active() -> None:
    body = json.loads(FIND_FIXTURE.read_text().replace("<CODIGO>", CODE))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/oauth/token":
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 172799})
        return httpx.Response(200, json=body)

    client = HotmartApiClient(
        credentials=_credentials(), transport=httpx.MockTransport(handler)
    )

    found = asyncio.run(client.find_coupon(PRODUCT_ID, CODE))

    assert found is not None
    assert found.status == "valid"
    assert found.active is None
    assert found.coupon_id == 4301687


# ── El worker ──────────────────────────────────────────────────────


def test_reserved_is_created_then_verified_seven_minutes_later() -> None:
    hotmart = FakeHotmart()
    hotmart.find_status = "valid"
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())

    assert [s["status"] for s in store.settles] == ["request_started", "created"]
    created = store.settles[-1]
    start_ms, end_ms = hotmart_coupon_window(T0, EXPIRES_AT)
    assert created["hotmart_coupon_id"] == 987
    assert created["hotmart_start_ms"] == start_ms
    assert created["hotmart_end_ms"] == end_ms
    assert created["next_attempt_at"] == (T0 + timedelta(minutes=7)).isoformat()
    claim_args = store.claim_calls[0]
    assert claim_args["lead_seconds"] == 15 * 60
    assert claim_args["validity_seconds"] == 24 * 3600
    assert claim_args["discount"] == 0.10
    assert claim_args["code_prefix"] == "ATT1"

    clock.now = T0 + timedelta(minutes=3)
    assert asyncio.run(worker.run_once()) == 0
    assert hotmart.count("GET", f"/coupon/product/{PRODUCT_ID}") == 0

    clock.now = T0 + timedelta(minutes=7)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "valid"
    assert hotmart.count("POST", "/coupon") == 1


def test_valid_without_active_is_valid_as_hotmart_answers() -> None:
    hotmart = FakeHotmart()
    assert hotmart.find_active is None
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())
    clock.now = T0 + timedelta(minutes=7)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "valid"
    assert hotmart.count("GET", f"/coupon/product/{PRODUCT_ID}") == 1


def test_valid_but_explicitly_inactive_is_create_failed() -> None:
    hotmart = FakeHotmart()
    hotmart.find_active = False
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())
    clock.now = T0 + timedelta(minutes=7)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "create_failed"
    assert store.row["failure_code"] == "hotmart_coupon_inactive"


def test_soon_is_checked_again_a_minute_later() -> None:
    hotmart = FakeHotmart()
    hotmart.find_status = "soon"
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())
    clock.now = T0 + timedelta(minutes=7)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "created"
    assert store.settles[-1]["next_attempt_at"] == (
        T0 + timedelta(minutes=8)
    ).isoformat()

    hotmart.find_status = "valid"
    clock.now = T0 + timedelta(minutes=8)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "valid"


def test_timeout_becomes_unknown_and_the_find_avoids_a_duplicate() -> None:
    hotmart = FakeHotmart()
    hotmart.create_failures.append(httpx.ReadTimeout("slow"))
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())

    assert store.row["status"] == "create_unknown"
    assert store.row["failure_code"] == "hotmart_coupon_create_timeout"
    assert store.settles[-1]["next_attempt_at"] == (
        T0 + timedelta(minutes=1)
    ).isoformat()

    clock.now = T0 + timedelta(minutes=1)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "created"
    assert store.row["hotmart_coupon_id"] == 987
    assert hotmart.count("POST", "/coupon") == 1
    assert store.settles[-1]["next_attempt_at"] == (
        T0 + timedelta(minutes=8)
    ).isoformat()


def test_unknown_not_found_in_hotmart_is_created_again() -> None:
    hotmart = FakeHotmart()
    store = FakeCouponStore()
    store.row["status"] = "create_unknown"
    worker = _worker(store, hotmart.client(), Clock(T0))

    asyncio.run(worker.run_once())

    assert [s["status"] for s in store.settles] == ["request_started", "created"]
    assert hotmart.count("POST", "/coupon") == 1


def test_bad_request_is_a_definitive_failure() -> None:
    hotmart = FakeHotmart()
    hotmart.create_failures.append(400)
    store = FakeCouponStore()
    worker = _worker(store, hotmart.client(), Clock(T0))

    asyncio.run(worker.run_once())

    assert store.row["status"] == "create_failed"
    assert store.row["failure_code"] == "hotmart_coupon_create_http_400"


@pytest.mark.parametrize("status", [500, 429])
def test_server_errors_are_transient(status: int) -> None:
    hotmart = FakeHotmart()
    hotmart.create_failures.append(status)
    store = FakeCouponStore()
    worker = _worker(store, hotmart.client(), Clock(T0))

    asyncio.run(worker.run_once())

    assert store.row["status"] == "create_unknown"
    assert store.row["failure_code"] == f"hotmart_coupon_create_http_{status}"


def test_created_coupon_missing_in_hotmart_fails() -> None:
    hotmart = FakeHotmart()
    store = FakeCouponStore()
    store.row.update(
        status="created", hotmart_start_ms=1, hotmart_end_ms=2, hotmart_coupon_id=9
    )
    worker = _worker(store, hotmart.client(), Clock(T0))

    asyncio.run(worker.run_once())

    assert store.row["status"] == "create_failed"
    assert store.row["failure_code"] == "hotmart_coupon_missing"


def test_token_failure_does_not_mark_request_started() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = HotmartApiClient(
        credentials=_credentials(), transport=httpx.MockTransport(handler)
    )
    store = FakeCouponStore()
    worker = _worker(store, client, Clock(T0))

    asyncio.run(worker.run_once())

    assert store.settles == []
    assert store.row["status"] == "reserved"


def test_logs_never_carry_the_secret_or_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    hotmart = FakeHotmart()
    hotmart.create_failures.append(httpx.ConnectError("down"))
    store = FakeCouponStore()
    clock = Clock(T0)
    worker = _worker(store, hotmart.client(), clock)

    asyncio.run(worker.run_once())
    clock.now = T0 + timedelta(minutes=1)
    asyncio.run(worker.run_once())
    clock.now = T0 + timedelta(minutes=8)
    asyncio.run(worker.run_once())

    assert store.row["status"] == "valid"
    assert caplog.records, "expected the worker and httpx to log"
    text = caplog.text
    for secret in (CLIENT_ID, CLIENT_SECRET, BASIC, ACCESS_TOKEN):
        assert secret not in text
    # El worker tampoco escribe el codigo del cupon (httpx si, en la URL del GET).
    worker_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bridge.recovery_coupons"
    ]
    assert worker_lines and all(CODE not in line for line in worker_lines)
    assert "client_secret=<redacted>" in text
    assert CLIENT_SECRET not in repr(_credentials())


# ── Las credenciales ───────────────────────────────────────────────


def test_credentials_file_is_read_with_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "hotmart.env"
    path.write_text(
        "# hotmart\n"
        f"HOTMART_CLIENT_ID={CLIENT_ID}\n"
        f'HOTMART_CLIENT_SECRET="{CLIENT_SECRET}"\n'
        f"export HOTMART_BASIC='Basic {BASIC}'\n"
    )

    credentials = load_hotmart_api_credentials(path)

    assert credentials.client_secret == CLIENT_SECRET
    assert credentials.basic == f"Basic {BASIC}"


def test_missing_credentials_are_named_without_values(tmp_path: Path) -> None:
    path = tmp_path / "hotmart.env"
    path.write_text(f"HOTMART_CLIENT_ID={CLIENT_ID}\nHOTMART_BASIC=\n")

    with pytest.raises(HotmartCredentialsError) as excinfo:
        load_hotmart_api_credentials(path)

    message = str(excinfo.value)
    assert "HOTMART_CLIENT_SECRET" in message
    assert "HOTMART_BASIC" in message
    assert CLIENT_ID not in message


def test_unreadable_credentials_file_has_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(HotmartCredentialsError, match="unreadable"):
        load_hotmart_api_credentials(tmp_path / "missing.env")


# ── Las RPC de Supabase ────────────────────────────────────────────


def _supabase(responses: list[tuple[int, object]]) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = pending.pop(0)
        return httpx.Response(status, json=body)

    return (
        SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="service-role",
            transport=httpx.MockTransport(handler),
        ),
        requests,
    )


def test_claim_rpc_sends_intervals_and_validates_rows() -> None:
    client, requests = _supabase(
        [
            (
                200,
                [
                    {
                        "coupon_id": COUPON_ID,
                        "recovery_case_id": CASE_ID,
                        "code": CODE,
                        "status": "reserved",
                        "external_product_id": PRODUCT_ID,
                        "expires_at": "2026-10-19T15:15:00+00:00",
                        "attempt_count": 1,
                    }
                ],
            )
        ]
    )

    claims = asyncio.run(
        client.claim_due_recovery_coupons(
            now=T0.isoformat(),
            worker_id="w",
            lease_seconds=120,
            code_prefix="ATT1",
            lead_seconds=900,
            validity_seconds=86400,
            discount=0.1,
            limit=10,
        )
    )

    assert claims[0].expires_at == EXPIRES_AT
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/rest/v1/rpc/claim_due_recovery_coupons_v1"
    assert body["p_lead"] == "900 seconds"
    assert body["p_validity"] == "86400 seconds"


def test_claim_rpc_rejects_an_unexpected_status() -> None:
    client, _ = _supabase(
        [
            (
                200,
                [
                    {
                        "coupon_id": COUPON_ID,
                        "recovery_case_id": CASE_ID,
                        "code": CODE,
                        "status": "valid",
                        "external_product_id": PRODUCT_ID,
                        "expires_at": "2026-10-19T15:15:00+00:00",
                        "attempt_count": 1,
                    }
                ],
            )
        ]
    )

    with pytest.raises(Exception, match="recovery_coupon_claim_invalid_row"):
        asyncio.run(
            client.claim_due_recovery_coupons(
                now=T0.isoformat(),
                worker_id="w",
                lease_seconds=120,
                code_prefix="ATT1",
                lead_seconds=900,
                validity_seconds=86400,
                discount=0.1,
                limit=10,
            )
        )


def test_settle_rpc_maps_a_lost_lease_to_a_permanent_error() -> None:
    client, _ = _supabase(
        [(400, {"code": "55000", "message": "recovery_coupon_lease_not_held"})]
    )

    with pytest.raises(SupabasePermanentError) as excinfo:
        asyncio.run(
            client.settle_recovery_coupon(
                coupon_id=COUPON_ID,
                worker_id="w",
                status="valid",
                now=T0.isoformat(),
            )
        )

    assert excinfo.value.reason == "recovery_coupon_lease_not_held"


def test_get_for_case_returns_none_or_the_coupon() -> None:
    client, requests = _supabase(
        [
            (200, []),
            (
                200,
                [
                    {
                        "coupon_id": COUPON_ID,
                        "code": CODE,
                        "status": "valid",
                        "expires_at": "2026-10-19T15:15:00+00:00",
                        "verified_at": "2026-10-18T15:07:00+00:00",
                    }
                ],
            ),
        ]
    )

    assert asyncio.run(client.get_recovery_coupon_for_case(recovery_case_id=CASE_ID)) is None
    coupon = asyncio.run(client.get_recovery_coupon_for_case(recovery_case_id=CASE_ID))

    assert coupon is not None and coupon.status == "valid"
    assert json.loads(requests[0].content) == {
        "p_recovery_case_id": CASE_ID,
        "p_ordinal": 1,
    }


# ── El arranque ────────────────────────────────────────────────────


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        supabase_base_url="https://supabase.example.test",
        supabase_service_role_key="test-service-role",
        **overrides,
    )


def test_flag_off_builds_no_worker_and_never_reads_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_read(path: object) -> object:
        raise AssertionError("the credentials file must not be read")

    monkeypatch.setattr(app_module, "load_hotmart_api_credentials", fail_if_read)
    settings = _settings(
        tmp_path, hotmart_api_credentials_file=tmp_path / "does-not-exist.env"
    )

    app = create_app(settings)

    assert settings.recovery_coupon_worker_enabled is False
    assert app.state.recovery_coupon_worker is None


def test_flag_defaults_from_env_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "RECOVERY_COUPON_WORKER_ENABLED",
        "RECOVERY_COUPON_CODE_PREFIX",
        "HOTMART_API_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.recovery_coupon_worker_enabled is False
    assert settings.recovery_coupon_code_prefix == "ATT1"
    assert settings.hotmart_api_credentials_file is None

    monkeypatch.setenv("RECOVERY_COUPON_WORKER_ENABLED", "true")
    monkeypatch.setenv("RECOVERY_COUPON_CODE_PREFIX", "LAN2")
    monkeypatch.setenv("HOTMART_API_CREDENTIALS_FILE", "/run/hotmart-api-credentials")

    enabled = Settings.from_env()

    assert enabled.recovery_coupon_worker_enabled is True
    assert enabled.recovery_coupon_code_prefix == "LAN2"
    assert enabled.hotmart_api_credentials_file == Path("/run/hotmart-api-credentials")


def test_flag_on_without_file_fails_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path, recovery_coupon_worker_enabled=True)

    with pytest.raises(ValueError, match="HOTMART_API_CREDENTIALS_FILE is required"):
        create_app(settings)


def test_flag_on_with_incomplete_file_names_the_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "hotmart.env"
    path.write_text(f"HOTMART_CLIENT_ID={CLIENT_ID}\n")
    settings = _settings(
        tmp_path,
        recovery_coupon_worker_enabled=True,
        hotmart_api_credentials_file=path,
    )

    with pytest.raises(HotmartCredentialsError) as excinfo:
        create_app(settings)

    assert "HOTMART_CLIENT_SECRET" in str(excinfo.value)
    assert CLIENT_ID not in str(excinfo.value)


@pytest.mark.parametrize("prefix", ["att1", "A", "TOOLONGPREFIX", "AT-1"])
def test_flag_on_rejects_an_invalid_prefix(tmp_path: Path, prefix: str) -> None:
    settings = _settings(
        tmp_path,
        recovery_coupon_worker_enabled=True,
        recovery_coupon_code_prefix=prefix,
    )

    with pytest.raises(ValueError, match="RECOVERY_COUPON_CODE_PREFIX"):
        create_app(settings)


def test_flag_on_with_file_builds_the_worker(tmp_path: Path) -> None:
    path = tmp_path / "hotmart.env"
    path.write_text(
        f"HOTMART_CLIENT_ID={CLIENT_ID}\n"
        f"HOTMART_CLIENT_SECRET={CLIENT_SECRET}\n"
        f"HOTMART_BASIC={BASIC}\n"
    )
    settings = _settings(
        tmp_path,
        recovery_coupon_worker_enabled=True,
        hotmart_api_credentials_file=path,
    )

    app = create_app(settings)

    assert isinstance(app.state.recovery_coupon_worker, RecoveryCouponWorker)
    assert "recovery_coupon_worker_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_flag_on_without_supabase_fails(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path, recovery_coupon_worker_enabled=True),
        supabase_base_url=None,
        supabase_service_role_key=None,
    )

    with pytest.raises(ValueError, match="Supabase is required"):
        create_app(settings)
