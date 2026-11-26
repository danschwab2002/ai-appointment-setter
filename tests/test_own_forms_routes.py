"""HTTP de los formularios propios: /f/<clave>, /f/<clave>/embed.js, /f/<clave>/enviar.

Contrato: docs/contracts/own-forms-v1.md. El envio de prueba usa el comprador del
fixture del evento real de ATT1 (tests/fixtures/own_forms/, 16/11) y la admision es
un fake de la RPC portable, como en test_ghl_precheckout_adapter_http: la misma
admision que usa el adaptador de GHL.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.supabase import SupabaseError
from test_own_forms import BUYER, att1_with_forms

BASE = "https://setter-att1.example"
ORIGIN = "https://site.metodoraizana.com.mx"
FORMS = [
    {
        "clave": "att1-org-b",
        "landing_id": "alimenta-tu-tiroides",
        "origenes": [ORIGIN],
        "webhooks": ["https://hooks.example/ghl-inbound"],
    }
]
MANIFEST = att1_with_forms(FORMS)


class _PortableFake:
    def __init__(self, outcome: str = "inserted", *, fail: bool = False) -> None:
        self.outcome = outcome
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return None

    async def admit_portable_observed_lead_precheckout(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.fail:
            raise SupabaseError("portable_observed_lead_precheckout_admission failed")
        return SimpleNamespace(
            outcome=self.outcome,
            submission_id="3a0f1d9e-6f5b-4b7e-9d5c-1d2a3b4c5d6e",
            purchase_intent_id="7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
        )


def _settings(**overrides: object) -> Settings:
    settings = Settings(
        webhook_secret="chatwoot-webhook-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/own-forms-http"),
        max_age_seconds=300,
        commercial_ally_config=MANIFEST.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
        instance_manifest=MANIFEST,
        hermes_model_name=MANIFEST.agent_model_name,
        own_forms_enabled=True,
        own_forms_public_base_url=BASE,
    )
    return replace(settings, **overrides)


def _client(settings: Settings | None = None, fake: object | None = None):
    fake = _PortableFake() if fake is None else fake
    app = create_app(settings or _settings(), supabase_client=fake)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE), fake


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "nombre": BUYER["name"],
        "email": BUYER["email"],
        "telefono": BUYER["phone"],
        "pagina": f"{ORIGIN}/alimenta-tu-tiroides?utm_source=meta-ads",
        "referrer": "",
        "website": "",
        "fbc": "",
        "fbp": "",
    }
    body.update(overrides)
    return body


def _run(coro):
    return asyncio.run(coro)


async def _request(method: str, path: str, *, settings=None, fake=None, **kwargs):
    client, fake = _client(settings, fake)
    async with client:
        response = await client.request(method, path, **kwargs)
    return response, fake


def _submit(body: object | None = None, *, origin: str = ORIGIN, **kwargs):
    headers = {"Content-Type": "application/json", "Origin": origin}
    headers.update(kwargs.pop("headers", {}))
    return _run(
        _request(
            "POST",
            "/f/att1-org-b/enviar",
            content=json.dumps(_body() if body is None else body),
            headers=headers,
            **kwargs,
        )
    )


def test_flag_off_means_404_everywhere() -> None:
    settings = _settings(own_forms_enabled=False)
    for method, path in (
        ("GET", "/f/att1-org-b"),
        ("GET", "/f/att1-org-b/embed.js"),
        ("POST", "/f/att1-org-b/enviar"),
        ("OPTIONS", "/f/att1-org-b/enviar"),
    ):
        response, fake = _run(_request(method, path, settings=settings))
        assert response.status_code == 404, (method, path)
        assert fake.calls == []


def test_unknown_form_is_404() -> None:
    response, _ = _run(_request("GET", "/f/no-existe"))
    assert response.status_code == 404


def test_page_and_embed_are_served() -> None:
    page, _ = _run(_request("GET", "/f/att1-org-b"))
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert f'<script src="{BASE}/f/att1-org-b/embed.js"></script>' in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    script, _ = _run(_request("GET", "/f/att1-org-b/embed.js"))
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert f"{BASE}/f/att1-org-b/enviar" in script.text


def test_preflight_only_for_declared_origins() -> None:
    ok, _ = _run(
        _request("OPTIONS", "/f/att1-org-b/enviar", headers={"Origin": ORIGIN})
    )
    assert ok.status_code == 204
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    bad, _ = _run(
        _request("OPTIONS", "/f/att1-org-b/enviar", headers={"Origin": "https://evil.example"})
    )
    assert bad.status_code == 403
    assert "access-control-allow-origin" not in bad.headers


def test_submission_is_admitted_like_the_ghl_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict]] = []

    async def fake_post(self, url, json=None, **kwargs):  # noqa: A002
        sent.append((url, json))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    response, fake = _submit()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "received"
    assert body["checkout_url"].startswith("https://pay.hotmart.com/D98014973Y?off=bmaztyhg&")
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert len(fake.calls) == 1
    call = fake.calls[0]
    event = call["raw_payload"]
    assert event["source"]["landing_id"] == "alimenta-tu-tiroides"
    assert event["data"]["consent"]["copy_version"] == "att1-whatsapp-contact-v1"
    assert event["data"]["attribution"]["utm_source"] == "meta-ads"
    assert call["external_submission_id"] == event["id"]
    # La copia al webhook declarado (paridad con la accion Webhook de GHL).
    assert [url for url, _ in sent] == ["https://hooks.example/ghl-inbound"]
    assert sent[0][1]["contact"]["email"] == BUYER["email"]


def test_hosted_page_origin_is_allowed() -> None:
    response, fake = _submit(origin=BASE)
    assert response.status_code == 200
    assert len(fake.calls) == 1


def test_other_origin_is_refused_without_admission() -> None:
    response, fake = _submit(origin="https://evil.example")
    assert response.status_code == 403
    assert fake.calls == []


def test_bad_phone_is_422_with_message() -> None:
    response, fake = _submit(_body(telefono="+52123"))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["codigo"] == "telefono" and detail["mensaje"]
    assert fake.calls == []


def test_honeypot_answers_ok_and_stores_nothing() -> None:
    response, fake = _submit(_body(website="http://spam"))
    assert response.status_code == 200
    assert response.json()["checkout_url"] is None
    assert fake.calls == []


def test_wrong_content_type_and_bad_json() -> None:
    response, fake = _run(
        _request(
            "POST",
            "/f/att1-org-b/enviar",
            content="nombre=x",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN},
        )
    )
    assert response.status_code == 415
    response, fake = _run(
        _request(
            "POST",
            "/f/att1-org-b/enviar",
            content="{",
            headers={"Content-Type": "application/json", "Origin": ORIGIN},
        )
    )
    assert response.status_code == 400
    assert fake.calls == []


def test_phone_limit_is_three_per_hour() -> None:
    async def scenario():
        client, fake = _client()
        statuses = []
        async with client:
            for _ in range(4):
                reply = await client.post(
                    "/f/att1-org-b/enviar",
                    content=json.dumps(_body()),
                    headers={"Content-Type": "application/json", "Origin": ORIGIN},
                )
                statuses.append(reply.status_code)
        return statuses, fake

    statuses, fake = _run(scenario())
    assert statuses == [200, 200, 200, 429]
    assert len(fake.calls) == 3


def test_database_failure_is_503() -> None:
    response, _ = _submit(fake=_PortableFake(fail=True))
    assert response.status_code == 503


def test_flag_on_without_forms_does_not_start() -> None:
    manifest = att1_with_forms([])
    with pytest.raises(ValueError, match="own_forms_enabled->formularios_propios"):
        create_app(
            _settings(instance_manifest=manifest), supabase_client=_PortableFake()  # type: ignore[arg-type]
        )


def test_ready_reports_the_forms_only_with_the_flag() -> None:
    on, _ = _run(_request("GET", "/ready"))
    off, _ = _run(_request("GET", "/ready", settings=_settings(own_forms_enabled=False)))
    assert on.json().get("own_forms") == "enabled:1", on.text
    assert "own_forms" not in off.json()
