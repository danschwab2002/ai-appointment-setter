"""HTTP del adaptador de GHL: POST /webhooks/adapters/ghl/lead-precheckout.

Contrato: docs/contracts/ghl-precheckout-adapter-v1.md.

Fixtures: tests/fixtures/ghl/, las dos capturas del 2026-09-29 del receptor
ghl-capture-att1 (anonimizadas, con fecha y origen en su `_capture`), con los headers
que mando GHL (`headers`). Son los unicos payloads reales de GHL: toda variante de
esta suite se DERIVA de ellos (el token en customData, cambiar mediumId o la URL,
quitar un campo, mover un objeto de atribucion real, repetir la entrega, repetir una
clave). Ninguna esta escrita de cero. Goldens: tests/fixtures/ghl/expected/.

La admision es un fake de la RPC portable: la admision real contra PGlite es
tests/sql/followup_engine/validate_ghl_precheckout_adapter.mjs.

Al final, el adaptador con el primer contacto del formulario: solo arranca con la
aceptacion escrita del riesgo en [adaptadores.ghl] (la de estos tests es de
prueba), y entonces el envio de GHL admite y planifica en una sola RPC.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import shutil
import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings, create_app
from bridge.instance_manifest import GhlRiskAcceptance, InstanceManifest
from bridge.supabase import SupabaseError
from test_instance_wiring import (
    ATT1 as ATT1_INSTANCE,
    _FirstContactAuthority,
    _first_contact_app,
    _first_contact_settings,
)

FIXTURES = Path(__file__).parent / "fixtures"
GHL = FIXTURES / "ghl"
EXPECTED = GHL / "expected"
ATT1_TOML = FIXTURES / "instances" / "att1" / "instancia.toml"
PATH = "/webhooks/adapters/ghl/lead-precheckout"

ADS_A_FORM = "EgDqRl2xWc59YjVW1q8W"
LANDING_D_FORM = "Om5FpIg5Sr5ce7nSkuPy"
TOKEN = "ghl-adapter-test-token-0123456789abcdef"
ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
LANDING_D_SCK = (
    "meta-ads~ig~drn_att_pay_evg_capt_Ad155_sept26~Mx-Tiroides-con-Hambre"
    "~30-SR-ATT-Test-Ads-MX-Ciudades-2809~120250442190930484"
)
# Claves del cuerpo de GHL que nunca pueden llegar a la base: raw_payload es el
# evento traducido.
GHL_ONLY_KEYS = {
    "contact_id",
    "first_name",
    "last_name",
    "full_name",
    "tags",
    "country",
    "timezone",
    "date_created",
    "contact_source",
    "location",
    "workflow",
    "triggerData",
    "contact",
    "attributionSource",
    "customData",
}


def _manifest(forms: list[str]) -> InstanceManifest:
    # El fixture de ATT1 es copia de la instancia y todavia no tiene el adaptador:
    # la seccion se arma por mutacion, como en test_instance_manifest.
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["eventos"] = [*payload["eventos"], "intencion"]
    payload["adaptadores"] = {"ghl": {"formularios": forms}}
    return InstanceManifest.from_mapping(payload)


# ATT1 tal como arranca (D4 y E14: Om5F todavia no entra) y con los dos formularios
# medidos, para cubrir la landing -d.
ATT1_ONLY_EGDQ = _manifest([ADS_A_FORM])
ATT1_BOTH_FORMS = _manifest([ADS_A_FORM, LANDING_D_FORM])


class _PortableFake:
    """La RPC portable: registra cada admision y responde el outcome pedido."""

    def __init__(self, outcome: str = "inserted", *, fail: bool = False) -> None:
        self.outcome = outcome
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def admit_portable_observed_lead_precheckout(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.fail:
            raise SupabaseError("portable_observed_lead_precheckout_admission failed")
        return SimpleNamespace(
            outcome=self.outcome,
            submission_id="3a0f1d9e-6f5b-4b7e-9d5c-1d2a3b4c5d6e",
            purchase_intent_id="7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
        )


def _settings(manifest: InstanceManifest = ATT1_ONLY_EGDQ, **overrides: object) -> Settings:
    settings = Settings(
        webhook_secret="chatwoot-webhook-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/ghl-adapter-http"),
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        ghl_precheckout_adapter_enabled=True,
        ghl_precheckout_adapter_token=TOKEN,
    )
    return replace(settings, **overrides)


def _app(
    settings: Settings | None = None, supabase: object | None = None
) -> tuple[object, _PortableFake | None]:
    fake = _PortableFake() if supabase is None else supabase
    return create_app(settings or _settings(), supabase_client=fake), fake  # type: ignore[arg-type]


def _capture(name: str) -> dict:
    return json.loads((GHL / name).read_text(encoding="utf-8"))


ADS_A_CAPTURE = _capture("ghl_form_webhook_att1_ads_a_20260929.json")
EDITOR_CAPTURE = _capture("ghl_form_webhook_editor_test_landing_d_20260929.json")
# Los headers que mando GHL en las dos capturas.
GHL_HEADERS: dict[str, str] = dict(ADS_A_CAPTURE["headers"])
assert GHL_HEADERS == EDITOR_CAPTURE["headers"]


def _ads_a() -> dict:
    return copy.deepcopy(ADS_A_CAPTURE["payload"])


def _editor() -> dict:
    return copy.deepcopy(EDITOR_CAPTURE["payload"])


def _landing_d() -> dict:
    """La prueba del editor con el ultimo toque real del mismo contacto movido al
    primer nivel: la forma de un envio real del formulario Om5F."""

    body = _editor()
    body["attributionSource"] = copy.deepcopy(body["contact"]["lastAttributionSource"])
    return body


def _with_body_token(body: dict, token: object = TOKEN) -> dict:
    # La accion Webhook estandar de GHL manda Custom Data en customData ({} en las
    # capturas).
    body["customData"] = {**body["customData"], "setter_token": token}
    return body


def _raw(body: object) -> bytes:
    # JSON.stringify de axios: compacto y en UTF-8.
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _post(
    app: object,
    body: object,
    *,
    header_token: str | None = TOKEN,
    content_type: str | None = GHL_HEADERS["Content-Type"],
    raw: bytes | None = None,
) -> httpx.Response:
    content = raw if raw is not None else _raw(body)
    headers = {"User-Agent": GHL_HEADERS["User-Agent"]}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if header_token is not None:
        headers["X-Setter-Adapter-Token"] = header_token

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(PATH, content=content, headers=headers)

    return asyncio.run(send())


def _golden(name: str) -> dict:
    return json.loads((EXPECTED / name).read_text(encoding="utf-8"))


def _without(mapping: dict, *keys: str) -> dict:
    return {key: value for key, value in mapping.items() if key not in keys}


def _assert_admitted_as_golden(call: dict, golden_name: str) -> None:
    """Lo que llego a la RPC es el golden, salvo el id aleatorio y la hora."""

    golden = _golden(golden_name)
    raw_payload = call["raw_payload"]
    canonical = call["canonical_payload"]
    assert _without(raw_payload, "id", "created_at") == _without(  # type: ignore[arg-type]
        golden["raw_payload"], "id", "created_at"
    )
    assert _without(canonical, "external_submission_id", "submitted_at") == _without(  # type: ignore[arg-type]
        golden["canonical_payload"], "external_submission_id", "submitted_at"
    )
    assert call["external_submission_id"] == raw_payload["id"]  # type: ignore[index]
    assert canonical["external_submission_id"] == raw_payload["id"]  # type: ignore[index]
    assert ULID.fullmatch(raw_payload["id"])  # type: ignore[index, arg-type]
    created_at = datetime.fromisoformat(raw_payload["created_at"])  # type: ignore[index, arg-type]
    assert abs(datetime.now(UTC) - created_at) < timedelta(minutes=1)


# -------------------------------------------------------------- envio real ads-a


def test_ads_a_real_submission_with_the_header_token_is_admitted() -> None:
    app, fake = _app()

    response = _post(app, _ads_a())

    assert response.status_code == 200
    body = response.json()
    assert fake is not None and len(fake.calls) == 1
    call = fake.calls[0]
    assert body == {
        "status": "received",
        "delivery_id": call["external_submission_id"],
        "purchase_intent_id": "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
        "activation_authorized": False,
        "contact_authorized": False,
    }
    # La admision portable con el binding del runtime, nunca la vieja.
    assert call["config"] == _settings().commercial_ally_config
    canonical = call["canonical_payload"]
    assert canonical["commerce"]["offer_ref"] == "gopi6lh7"  # type: ignore[index]
    assert canonical["source"]["landing_ref"] == "ads-a"  # type: ignore[index]
    assert canonical["identity"]["phone_valid"] is True  # type: ignore[index]
    assert canonical["consent"]["whatsapp_contact"] is True  # type: ignore[index]
    raw_payload = call["raw_payload"]
    # ?test=yes no pasa, y nada del cuerpo de GHL llega a la base.
    assert raw_payload["source"]["page_url"] == (  # type: ignore[index]
        "https://www.metodoraizana.com/att1/evg/vsl/ads-a"
    )
    assert raw_payload["event"] == "lead.precheckout"  # type: ignore[index]
    assert GHL_ONLY_KEYS.isdisjoint(raw_payload)  # type: ignore[arg-type]
    assert "test=yes" not in json.dumps(raw_payload)
    _assert_admitted_as_golden(
        call, "ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json"
    )


def test_the_token_in_custom_data_alone_is_enough() -> None:
    app, fake = _app()

    response = _post(app, _with_body_token(_ads_a()), header_token=None)

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert fake is not None and len(fake.calls) == 1
    # El token no se guarda: customData no pasa al evento.
    call = fake.calls[0]
    assert TOKEN not in json.dumps([call["raw_payload"], call["canonical_payload"]])


def test_the_same_token_in_the_header_and_custom_data_is_admitted() -> None:
    app, fake = _app()

    response = _post(app, _with_body_token(_ads_a()))

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1


# ------------------------------------------------------------------- el token


@pytest.mark.parametrize(
    ("header_token", "body_token"),
    [
        (None, None),
        ("", None),
        (TOKEN + "x", None),
        (TOKEN[:-1], None),
        (None, TOKEN + "x"),
        (None, ""),
        (None, 12345),
        (TOKEN, TOKEN + "x"),
        (TOKEN, ""),
        (TOKEN + "x", TOKEN),
        ("chatwoot-webhook-secret", None),
    ],
    ids=[
        "sin-token",
        "header-vacio",
        "header-distinto",
        "header-corto",
        "cuerpo-distinto",
        "cuerpo-vacio",
        "cuerpo-no-texto",
        "header-bien-cuerpo-distinto",
        "header-bien-cuerpo-vacio",
        "header-distinto-cuerpo-bien",
        "otro-secreto",
    ],
)
def test_a_missing_or_different_token_is_a_401(
    header_token: str | None, body_token: object
) -> None:
    body = _ads_a() if body_token is None else _with_body_token(_ads_a(), body_token)
    app, fake = _app()

    response = _post(app, body, header_token=header_token)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_adapter_token"
    assert fake is not None and fake.calls == []


def test_a_different_header_is_a_401_without_reading_the_body() -> None:
    # El cuerpo real mas de 64 KiB de espacios al final: con el header correcto es 413.
    # Con un header distinto es 401, asi que el cuerpo no se leyo.
    oversized = _raw(_ads_a()) + b" " * (65 * 1024)
    app, fake = _app()

    rejected = _post(app, None, header_token=TOKEN + "x", raw=oversized)
    too_large = _post(app, None, raw=oversized)

    assert rejected.status_code == 401
    assert too_large.status_code == 413
    assert too_large.json()["detail"] == "ghl_adapter_body_too_large"
    assert fake is not None and fake.calls == []


# ---------------------------------------------------------- transporte y cuerpo


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "Application/JSON"],
)
def test_the_media_type_is_json_with_or_without_charset(content_type: str) -> None:
    app, fake = _app()

    response = _post(app, _ads_a(), content_type=content_type)

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
def test_another_media_type_is_a_400(content_type: str | None) -> None:
    app, fake = _app()

    response = _post(app, _ads_a(), content_type=content_type)

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_ghl_transport"
    assert fake is not None and fake.calls == []


def test_the_body_limit_is_64_kib() -> None:
    real = _raw(_ads_a())
    limit = app_module.PRECHECKOUT_WEBHOOK_BODY_LIMIT_BYTES
    app, fake = _app()

    at_the_limit = _post(app, None, raw=real + b" " * (limit - len(real)))
    over_the_limit = _post(app, None, raw=real + b" " * (limit - len(real) + 1))

    assert at_the_limit.status_code == 200
    assert over_the_limit.status_code == 413
    assert fake is not None and len(fake.calls) == 1


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (_raw(ADS_A_CAPTURE["payload"])[:-1], "ghl_invalid_json"),
        (b"", "ghl_invalid_json"),
        (b"[" + _raw(ADS_A_CAPTURE["payload"]) + b"]", "ghl_invalid_payload"),
        # Un campo personalizado que se llame como un campo estandar (E8).
        (
            _raw(ADS_A_CAPTURE["payload"]).replace(
                b"{", b'{"email":"otra.persona@example.com",', 1
            ),
            "ghl_invalid_payload",
        ),
    ],
    ids=["cortado", "vacio", "lista", "email-repetido"],
)
def test_a_body_that_is_not_one_json_object_is_a_400(raw: bytes, detail: str) -> None:
    app, fake = _app()

    response = _post(app, None, raw=raw)

    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert fake is not None and fake.calls == []


def test_a_repeated_key_never_picks_the_token(caplog: pytest.LogCaptureFixture) -> None:
    # Dos customData: el adaptador no elige en silencio cual trae el token. Sin header
    # el pedido no esta autenticado: 401, con el motivo real en el log. Con el header
    # correcto, el 400 de siempre.
    raw = _raw(_with_body_token(_ads_a())).replace(
        b'"customData":', b'"customData":{},"customData":', 1
    )
    app, fake = _app()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        unauthenticated = _post(app, None, header_token=None, raw=raw)
        authenticated = _post(app, None, raw=raw)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "invalid_adapter_token"
    assert authenticated.status_code == 400
    assert authenticated.json()["detail"] == "ghl_invalid_payload"
    assert fake is not None and fake.calls == []
    lines = _adapter_lines(caplog)
    assert "status=401 reason=invalid_adapter_token/ghl_invalid_payload " in lines[0]
    assert "status=400 reason=ghl_invalid_payload " in lines[1]


@pytest.mark.parametrize(
    ("content_type", "raw", "real_reason"),
    [
        ("text/plain", _raw(ADS_A_CAPTURE["payload"]), "invalid_ghl_transport"),
        (
            "application/json",
            _raw(ADS_A_CAPTURE["payload"]) + b" " * (65 * 1024),
            "ghl_adapter_body_too_large",
        ),
        ("application/json", _raw(ADS_A_CAPTURE["payload"])[:-1], "ghl_invalid_json"),
        (
            "application/json",
            b"[" + _raw(ADS_A_CAPTURE["payload"]) + b"]",
            "ghl_invalid_payload",
        ),
    ],
    ids=["otro-content-type", "cuerpo-grande", "json-cortado", "no-es-un-objeto"],
)
def test_without_the_header_a_body_that_cannot_be_read_is_only_a_401(
    content_type: str, raw: bytes, real_reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Quien no tiene el token no distingue por que no se leyo el cuerpo: el endpoint
    # no es un oraculo de parseo. El operador si, en la linea de log.
    app, fake = _app()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, None, header_token=None, content_type=content_type, raw=raw)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_adapter_token"}
    assert fake is not None and fake.calls == []
    (line,) = _adapter_lines(caplog)
    assert f"status=401 reason=invalid_adapter_token/{real_reason} " in line


@pytest.mark.parametrize("field", ["email", "phone", "contact_id"])
def test_a_contact_without_its_fields_is_a_400(field: str) -> None:
    body = _ads_a()
    body.pop(field)
    app, fake = _app()

    response = _post(app, body)

    assert response.status_code == 400
    assert response.json()["detail"] == "ghl_invalid_payload"
    assert fake is not None and fake.calls == []


# -------------------------------------------------------------- rechazos 422


def _ads_a_on(url: str) -> dict:
    body = _ads_a()
    body["attributionSource"]["url"] = url
    return body


@pytest.mark.parametrize(
    ("body", "manifest", "detail"),
    [
        # La prueba del editor de GHL: attributionSource de primer nivel {} (D3).
        (_editor(), ATT1_BOTH_FORMS, "ghl_not_a_form_submission"),
        # Om5F todavia no esta en la lista de ATT1 (D4, E14).
        (_landing_d(), ATT1_ONLY_EGDQ, "ghl_form_not_allowed"),
        (
            _ads_a_on("https://www.metodoraizana.com/att1/evg/vsl/ads-z?test=yes"),
            ATT1_ONLY_EGDQ,
            "ghl_landing_unknown",
        ),
        (
            _ads_a_on("https://www.metodoraizana.com/ATT1/evg/vsl/ads-a?test=yes"),
            ATT1_ONLY_EGDQ,
            "ghl_landing_unknown",
        ),
    ],
    ids=["prueba-del-editor", "formulario-fuera-de-la-lista", "landing-desconocida", "ruta-en-mayusculas"],
)
def test_a_body_that_is_not_an_allowed_submission_is_a_422_without_admission(
    body: dict, manifest: InstanceManifest, detail: str
) -> None:
    app, fake = _app(_settings(manifest))

    response = _post(app, body)

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert fake is not None and fake.calls == []


def test_an_unusable_phone_is_a_422_and_logs_only_its_region(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _ads_a()
    body["phone"] = body["phone"][:6]
    app, fake = _app()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "ghl_phone_unusable"
    assert fake is not None and fake.calls == []
    (line,) = _adapter_lines(caplog)
    assert "reason=ghl_phone_unusable" in line
    assert "phone_region=AR" in line
    assert body["phone"] not in line


def test_a_translation_the_parser_rejects_is_a_422_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Un error del traductor: reintentar no lo arregla, asi que no es 5xx.
    monkeypatch.setattr(app_module, "parse_lead_precheckout", lambda *a, **k: None)
    app, fake = _app()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _ads_a())

    assert response.status_code == 422
    assert response.json()["detail"] == "ghl_translation_rejected"
    assert fake is not None and fake.calls == []
    (record,) = [r for r in caplog.records if r.getMessage().startswith("ghl_precheckout_adapter ")]
    assert record.levelno == logging.WARNING


def test_a_submission_the_parser_marks_phone_invalid_never_reaches_the_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defensa: la admision portable 1.1.0 lo rechazaria con 22023, que llega como 503
    # y GHL reintentaria para siempre.
    real_parse = app_module.parse_lead_precheckout

    def parse_with_invalid_phone(*args: object, **kwargs: object) -> object:
        submission = real_parse(*args, **kwargs)  # type: ignore[arg-type]
        assert submission is not None
        return replace(submission, phone_valid=False)

    monkeypatch.setattr(app_module, "parse_lead_precheckout", parse_with_invalid_phone)
    app, fake = _app()

    response = _post(app, _ads_a())

    assert response.status_code == 422
    assert response.json()["detail"] == "ghl_phone_unusable"
    assert fake is not None and fake.calls == []


# ------------------------------------------------------------- la landing -d


def test_landing_d_submission_is_admitted_to_its_offer_with_the_composed_sck() -> None:
    app, fake = _app(_settings(ATT1_BOTH_FORMS))

    response = _post(app, _landing_d())

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    call = fake.calls[0]
    canonical = call["canonical_payload"]
    assert canonical["commerce"]["offer_ref"] == "2uafw5bg"  # type: ignore[index]
    assert canonical["source"]["landing_ref"] == "alimenta-tu-tiroides-d"  # type: ignore[index]
    attribution = call["raw_payload"]["data"]["attribution"]  # type: ignore[index]
    assert attribution["sck"] == LANDING_D_SCK
    assert len(attribution["fbclid"]) == 165
    assert call["raw_payload"]["source"]["site"] == "metodoraizana-mx"  # type: ignore[index]
    _assert_admitted_as_golden(
        call, "ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json"
    )


def test_a_mexican_mobile_with_the_legacy_1_is_admitted_as_plus_52() -> None:
    # E3: GHL podria guardar el movil mexicano con el 1 heredado.
    body = _landing_d()
    assert body["phone"].startswith("+52") and len(body["phone"]) == 13
    body["phone"] = "+521" + body["phone"][3:]
    app, fake = _app(_settings(ATT1_BOTH_FORMS))

    response = _post(app, body)

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    identity = fake.calls[0]["canonical_payload"]["identity"]  # type: ignore[index]
    assert identity["phone"] == "52" + body["phone"][4:]
    assert identity["phone_valid"] is True


def test_a_nul_or_a_lone_surrogate_never_reaches_the_rpc() -> None:
    # U+0000 en la URL (?utm_campaign=%00, que cualquiera escribe) y un surrogate
    # suelto en el nombre, que JSON permite escapado. Los dos harian fallar la RPC en
    # cada entrega (22P05 o UnicodeEncodeError), y GHL reintentaria sin fin.
    body = _ads_a()
    body["attributionSource"]["url"] += "&utm_campaign=a%00b&utm_source=fb"
    body["full_name"] = "Lead \ud800Anonimo"
    raw = json.dumps(body, separators=(",", ":")).encode("ascii")
    assert b"\\ud800" in raw
    app, fake = _app()

    response = _post(app, None, raw=raw)

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    call = fake.calls[0]
    sent = json.dumps([call["raw_payload"], call["canonical_payload"]], ensure_ascii=False)
    sent.encode("utf-8")
    assert "\u0000" not in sent
    assert call["raw_payload"]["data"]["attribution"]["utm_campaign"] == "ab"  # type: ignore[index]
    assert call["raw_payload"]["data"]["buyer"]["name"] == "Lead Anonimo"  # type: ignore[index]


# ---------------------------------------------------- admision y reintentos


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("inserted", "received"), ("duplicate", "duplicate"), ("semantic_conflict", "conflict")],
)
def test_the_admission_outcome_maps_like_webhooks_lead(outcome: str, status: str) -> None:
    fake = _PortableFake(outcome)
    app, _ = _app(supabase=fake)

    response = _post(app, _ads_a())

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["activation_authorized"] is False
    assert response.json()["contact_authorized"] is False


def test_two_deliveries_of_the_same_submission_are_two_admissions() -> None:
    # E1: un reintento de GHL es otra submission, con su propio id aleatorio, que la
    # RPC enlaza a la misma intencion viva. Nunca el mismo id con otro created_at,
    # que dejaria un conflicto sin resolver que descarta el consentimiento.
    app, fake = _app()

    first = _post(app, _ads_a())
    second = _post(app, _ads_a())

    assert first.status_code == second.status_code == 200
    assert fake is not None and len(fake.calls) == 2
    ids = [call["external_submission_id"] for call in fake.calls]
    assert ids[0] != ids[1]
    assert [first.json()["delivery_id"], second.json()["delivery_id"]] == ids
    for call in fake.calls:
        assert ULID.fullmatch(call["external_submission_id"])  # type: ignore[arg-type]
    canonical = [
        _without(call["canonical_payload"], "external_submission_id", "submitted_at")  # type: ignore[arg-type]
        for call in fake.calls
    ]
    assert canonical[0] == canonical[1]


def test_an_unavailable_admission_is_a_503_so_ghl_retries() -> None:
    fake = _PortableFake(fail=True)
    app, _ = _app(supabase=fake)

    response = _post(app, _ads_a())

    assert response.status_code == 503
    assert response.json()["detail"] == "ghl_precheckout_persist_unavailable"
    assert len(fake.calls) == 1


def test_without_supabase_the_adapter_is_a_503() -> None:
    app = create_app(_settings())

    response = _post(app, _ads_a())

    assert response.status_code == 503
    assert response.json()["detail"] == "supabase_not_configured"


# --------------------------------------------------------- apagado y Johanna


@pytest.mark.parametrize("configured_token", [None, TOKEN])
def test_with_the_flag_off_the_route_is_a_503_before_anything_else(
    configured_token: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    app, fake = _app(
        _settings(
            ghl_precheckout_adapter_enabled=False,
            ghl_precheckout_adapter_token=configured_token,
        )
    )

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        for header_token in (TOKEN, None, "otro"):
            response = _post(app, _ads_a(), header_token=header_token)

            assert response.status_code == 503
            assert response.json()["detail"] == "ghl_precheckout_adapter_not_enabled"
    assert fake is not None and fake.calls == []
    # Apagado es configuracion, no un envio perdido: no sale como warning.
    assert {
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith("ghl_precheckout_adapter ")
    } == {logging.INFO}


def test_johanna_answers_503_on_the_new_route() -> None:
    # Johanna: sin manifiesto, con /webhooks/lead prendido. La ruta nueva existe y
    # responde 503; /webhooks/lead lo controla test_lead_precheckout_http.py, que
    # este cambio no edita.
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=Path("/tmp/unused"),
        max_age_seconds=300,
        lead_precheckout_enabled=True,
        lead_precheckout_secret="fixture-lead-secret",
    )
    fake = _PortableFake()
    app = create_app(settings, supabase_client=fake)  # type: ignore[arg-type]

    response = _post(app, _with_body_token(_ads_a()))

    assert response.status_code == 503
    assert response.json()["detail"] == "ghl_precheckout_adapter_not_enabled"
    assert fake.calls == []


# ------------------------------------------------------------------- los logs


def _adapter_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("ghl_precheckout_adapter ")
    ]


def test_one_log_line_per_request_and_never_a_value_of_the_lead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ads_a = _ads_a()
    landing_d = _landing_d()
    wrong_form = _ads_a()
    wrong_form["attributionSource"]["mediumId"] = LANDING_D_FORM
    requests: list[tuple[dict, str | None]] = [
        (ads_a, TOKEN),
        (_with_body_token(_ads_a()), None),
        (landing_d, TOKEN),
        (_editor(), TOKEN),
        (wrong_form, TOKEN),
        (_ads_a(), TOKEN + "x"),
        (_ads_a(), None),
    ]
    app, _ = _app(_settings(ATT1_ONLY_EGDQ))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        statuses = [_post(app, body, header_token=token).status_code for body, token in requests]

    assert statuses == [200, 200, 422, 422, 422, 401, 401]
    lines = _adapter_lines(caplog)
    assert len(lines) == len(requests)
    # El bridge no configura logging: bajo uvicorn solo los warnings llegan a la
    # salida del contenedor. Todo envio que no quedo admitido es warning.
    levels = [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith("ghl_precheckout_adapter ")
    ]
    assert levels == [logging.INFO] * 2 + [logging.WARNING] * 5
    assert lines[0].startswith("ghl_precheckout_adapter outcome=received status=200 reason=-")
    assert f"form={ADS_A_FORM}" in lines[0]
    assert "landing=metodoraizana/ads-a offer=gopi6lh7" in lines[0]
    assert "phone_region=AR has_utm=false has_fbclid=false" in lines[0]
    assert "outcome=rejected status=422 reason=ghl_form_not_allowed" in lines[2]
    # El id del formulario es publico: se loguea aunque no este en la lista.
    assert f"form={LANDING_D_FORM}" in lines[2] and f"form={LANDING_D_FORM}" in lines[4]
    assert "reason=ghl_not_a_form_submission" in lines[3]
    assert "reason=invalid_adapter_token" in lines[5] and "reason=invalid_adapter_token" in lines[6]
    everything = "\n".join(record.getMessage() for record in caplog.records)
    for body in (ads_a, landing_d):
        submission = body["attributionSource"]
        for secret in (
            body["email"],
            body["phone"],
            body["contact_id"],
            body["full_name"],
            body["first_name"],
            submission["ip"],
            submission["userAgent"],
            submission.get("fbEventId") or body["contact"]["attributionSource"]["fbEventId"],
        ):
            assert secret not in everything
    assert landing_d["attributionSource"]["fbclid"] not in everything
    assert "test=yes" not in everything and "utm_" not in everything
    assert TOKEN not in everything


@pytest.mark.parametrize(
    "medium_id",
    [lambda body: body["email"], lambda body: body["attributionSource"]["mediumId"] + "x"],
    ids=["el-email-del-lead", "id-de-21-caracteres"],
)
def test_a_medium_id_without_the_shape_of_a_form_id_is_never_logged(
    medium_id, caplog: pytest.LogCaptureFixture
) -> None:
    # El id del formulario se loguea porque es publico; cualquier otro valor en
    # mediumId viene del cuerpo y puede ser un dato del lead.
    body = _ads_a()
    value = medium_id(body)
    body["attributionSource"]["mediumId"] = value
    app, fake = _app()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, body)

    assert response.status_code == 422
    assert response.json()["detail"] == "ghl_form_not_allowed"
    assert fake is not None and fake.calls == []
    (line,) = _adapter_lines(caplog)
    assert " form=- " in line
    assert value not in "\n".join(record.getMessage() for record in caplog.records)


# ------------------------- el adaptador con el primer contacto del formulario
# El caso que el primer contacto dejo pendiente: el formulario que entra por GHL.
# Con [adaptadores.ghl] en el manifiesto, flujos.precheckout solo arranca con la
# aceptacion escrita del riesgo del adaptador. El set completo del primer contacto
# es el de test_instance_wiring.py; aca la entrada del formulario es el adaptador y
# el envio es la captura real de ads-a. La aceptacion es DE PRUEBA: ningun
# manifiesto real la recibe de este codigo.

TEST_RISK_ACCEPTANCE = GhlRiskAcceptance(
    accepted_by="aceptacion de prueba (tests)",
    accepted_on=date(2026, 10, 1),
    contract="ghl-precheckout-adapter-v1",
)
ADS_A_GOLDEN = "ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json"


@pytest.fixture
def att1_instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1_INSTANCE, target)
    return target


def _first_contact_through_ghl(
    instance: Path, *, accepted: bool = True, **overrides: object
) -> Settings:
    """El set del primer contacto con el adaptador de GHL como unica entrada."""
    base = _first_contact_settings(instance)
    assert base.instance_manifest is not None
    manifest = replace(
        base.instance_manifest,
        ghl_form_ids=(ADS_A_FORM,),
        ghl_risk_acceptance=TEST_RISK_ACCEPTANCE if accepted else None,
    )
    values: dict[str, object] = {
        "instance_manifest": manifest,
        "ghl_precheckout_adapter_enabled": True,
        "ghl_precheckout_adapter_token": TOKEN,
        "lead_precheckout_enabled": False,
        "lead_precheckout_secret": None,
    }
    values.update(overrides)
    return replace(base, **values)


def _first_contact_off(settings: Settings) -> Settings:
    # El mismo runtime sin el flag (y sin lo que solo el flag habilita: el modo
    # directo y la salida durable).
    return replace(
        settings,
        portable_precheckout_first_contact_enabled=False,
        dispatcher_approved_template_direct_enabled=False,
        dispatcher_outbound_enabled=False,
    )


def test_the_adapter_with_the_acceptance_and_the_flag_admits_and_plans_in_one_rpc(
    att1_instance: Path,
) -> None:
    settings = _first_contact_through_ghl(att1_instance)
    authority = _FirstContactAuthority()

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    # La RPC que admite y planifica, nunca la admision sola.
    assert authority.admission_calls == []
    [call] = authority.plan_calls
    assert set(call) == {
        "config", "external_submission_id", "raw_payload", "canonical_payload",
        "scope_key", "scope_version",
    }
    assert call["config"] == settings.commercial_ally_config
    assert (call["scope_key"], call["scope_version"]) == ("att1-primer-contacto", 1)
    # Lo que se admite es el evento traducido de la captura real, igual que sin el
    # flag: nada del cuerpo de GHL llega a la base.
    _assert_admitted_as_golden(call, ADS_A_GOLDEN)
    assert GHL_ONLY_KEYS.isdisjoint(call["raw_payload"])  # type: ignore[arg-type]
    assert call["canonical_payload"]["consent"]["whatsapp_contact"] is True  # type: ignore[index]
    # La respuesta a GHL no cambia: nunca dice que hay permiso de contacto.
    assert response.json() == {
        "status": "received",
        "delivery_id": call["external_submission_id"],
        "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
        "activation_authorized": False,
        "contact_authorized": False,
    }


def test_the_adapter_with_the_acceptance_and_the_flag_off_only_admits(
    att1_instance: Path,
) -> None:
    settings = _first_contact_off(_first_contact_through_ghl(att1_instance))
    authority = _FirstContactAuthority()

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    assert authority.plan_calls == []
    [call] = authority.admission_calls
    assert set(call) == {
        "config", "external_submission_id", "raw_payload", "canonical_payload",
    }
    _assert_admitted_as_golden(call, ADS_A_GOLDEN)


def test_without_the_acceptance_the_first_contact_through_the_adapter_does_not_start(
    att1_instance: Path,
) -> None:
    # El mismo set, completo, sin la aceptacion: no arranca, con el flag del primer
    # contacto prendido o apagado, porque flujos.precheckout esta en true.
    settings = _first_contact_through_ghl(att1_instance, accepted=False)

    for candidate in (settings, _first_contact_off(settings)):
        with pytest.raises(
            ValueError,
            match=re.escape(
                "[adaptadores.ghl] cannot run with flujos.precheckout on without the "
                "written risk acceptance"
            ),
        ):
            _first_contact_app(candidate, _FirstContactAuthority())


@pytest.mark.parametrize(
    ("plan_outcome", "plan_reason", "level"),
    [
        ("planned", "first_contact_scheduled", logging.INFO),
        ("not_planned", "pilot_runtime_not_armed", logging.INFO),
        ("plan_failed", "channel_identity_inbox_mismatch", logging.WARNING),
    ],
)
def test_ghl_gets_the_same_200_whatever_the_plan_and_the_logs_carry_no_lead_value(
    att1_instance: Path,
    caplog: pytest.LogCaptureFixture,
    plan_outcome: str,
    plan_reason: str,
    level: int,
) -> None:
    # Un plan que no procede no es un error para GHL: con 5xx reintentaria un
    # envio que ya quedo admitido.
    authority = _FirstContactAuthority()
    authority.plan = {"plan_outcome": plan_outcome, "plan_reason": plan_reason}
    app = _first_contact_app(_first_contact_through_ghl(att1_instance), authority)
    body = _ads_a()

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, body)

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert response.json()["contact_authorized"] is False
    # La linea del adaptador no cambia, y el plan sale en la suya.
    (adapter_line,) = _adapter_lines(caplog)
    assert adapter_line.startswith(
        "ghl_precheckout_adapter outcome=received status=200 reason=-"
    )
    [plan_record] = [
        record
        for record in caplog.records
        if record.getMessage().startswith("portable_precheckout_first_contact ")
    ]
    assert plan_record.levelno == level
    assert plan_record.getMessage() == (
        "portable_precheckout_first_contact admission=inserted "
        f"plan={plan_outcome} reason={plan_reason} "
        "submission_id=bfc778e7-5c9f-45e6-a910-651f92312157"
    )
    everything = "\n".join(record.getMessage() for record in caplog.records)
    for value in (body["email"], body["phone"], body["full_name"], body["contact_id"], TOKEN):
        assert value not in everything


def test_an_unavailable_admit_and_plan_is_a_503_so_ghl_retries(att1_instance: Path) -> None:
    class _Unavailable(_FirstContactAuthority):
        async def admit_and_plan_portable_lead_precheckout(self, **_: object) -> object:
            raise SupabaseError("portable_lead_precheckout_admission_and_plan_failed: HTTP 503")

    app = _first_contact_app(_first_contact_through_ghl(att1_instance), _Unavailable())

    response = _post(app, _ads_a())

    assert response.status_code == 503
    assert response.json()["detail"] == "ghl_precheckout_persist_unavailable"


def test_the_acceptance_alone_changes_nothing_of_the_admission() -> None:
    # ATT1 con la aceptacion y todo lo demas como hoy (flujos apagados): el mismo
    # envio se admite igual, por la RPC de siempre.
    accepted = replace(ATT1_ONLY_EGDQ, ghl_risk_acceptance=TEST_RISK_ACCEPTANCE)
    plain_app, plain = _app()
    accepted_app, with_acceptance = _app(_settings(accepted))

    responses = [_post(plain_app, _ads_a()), _post(accepted_app, _ads_a())]

    assert [response.status_code for response in responses] == [200, 200]
    assert plain is not None and with_acceptance is not None
    [plain_call], [accepted_call] = plain.calls, with_acceptance.calls
    for call in (plain_call, accepted_call):
        _assert_admitted_as_golden(call, ADS_A_GOLDEN)
    assert plain_call["config"] == accepted_call["config"]


# --------------------------------------- landing_por_formulario (2026-10-09)
#
# attributionSource.url es la pagina donde empezo la visita (medido en ATT1 el
# 2026-10-08). Los envios se derivan de la captura de Om5F cambiando el mediumId
# o el host y la ruta de su URL; la oferta bmaztyhg se reata a la landing B como
# en la instancia (instancia#26).

UHTA_FORM = "UHTaDn8feKiqALhjGFFF"
LANDING_B_URL = "https://site.metodoraizana.com.mx/alimenta-tu-tiroides"
FORM_LANDINGS = {LANDING_D_FORM: "alimenta-tu-tiroides-d", UHTA_FORM: "alimenta-tu-tiroides"}


def _att1_today(mode: str | None = None) -> InstanceManifest:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["eventos"] = [*payload["eventos"], "intencion"]
    for offer in payload["hotmart"]["ofertas"]:
        if offer["codigo"] == "bmaztyhg":
            offer.update(
                site="metodoraizana-mx", landing_id="alimenta-tu-tiroides", url=LANDING_B_URL
            )
    ghl: dict = {"formularios": [ADS_A_FORM, LANDING_D_FORM, UHTA_FORM]}
    if mode is not None:
        ghl["landing_por_formulario"] = FORM_LANDINGS
        ghl["landing_por_formulario_modo"] = mode
    payload["adaptadores"] = {"ghl": ghl}
    return InstanceManifest.from_mapping(payload)


def _uhta_from_landing_d() -> dict:
    body = _landing_d()
    body["attributionSource"]["mediumId"] = UHTA_FORM
    return body


def _om5f_from_org_a() -> dict:
    body = _landing_d()
    query = body["attributionSource"]["url"].split("?", 1)[1]
    body["attributionSource"]["url"] = f"https://www.metodoraizana.com/att1/evg/vsl/org-a?{query}"
    return body


def _one_adapter_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("ghl_precheckout_adapter ")
    ]
    return record


def test_shadow_admits_with_the_url_landing_and_logs_the_fix_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, fake = _app(_settings(_att1_today("sombra")))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _uhta_from_landing_d())

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    canonical = fake.calls[0]["canonical_payload"]
    assert canonical["commerce"]["offer_ref"] == "2uafw5bg"  # type: ignore[index]
    record = _one_adapter_record(caplog)
    # Admitido, pero warning: bajo uvicorn solo los warnings llegan a la salida
    # del contenedor, y es lo que se cuenta para decidir si se activa.
    assert record.levelno == logging.WARNING
    line = record.getMessage()
    assert "outcome=received status=200 reason=-" in line
    assert f"form={UHTA_FORM}" in line
    assert "landing=metodoraizana-mx/alimenta-tu-tiroides-d offer=2uafw5bg" in line
    assert line.endswith("form_landing=would_fix:alimenta-tu-tiroides")


def test_active_admits_with_the_landing_of_the_form(caplog: pytest.LogCaptureFixture) -> None:
    app, fake = _app(_settings(_att1_today("activo")))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _uhta_from_landing_d())

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    call = fake.calls[0]
    canonical = call["canonical_payload"]
    assert canonical["commerce"]["offer_ref"] == "bmaztyhg"  # type: ignore[index]
    assert canonical["source"]["landing_ref"] == "alimenta-tu-tiroides"  # type: ignore[index]
    assert call["raw_payload"]["source"]["page_url"] == LANDING_B_URL  # type: ignore[index]
    # La atribucion es la de la URL de la visita.
    assert call["raw_payload"]["data"]["attribution"]["sck"] == LANDING_D_SCK  # type: ignore[index]
    record = _one_adapter_record(caplog)
    assert record.levelno == logging.WARNING
    line = record.getMessage()
    assert "landing=metodoraizana-mx/alimenta-tu-tiroides offer=bmaztyhg" in line
    assert line.endswith("form_landing=fixed:alimenta-tu-tiroides")


@pytest.mark.parametrize(
    ("mode", "status", "note"),
    [
        (None, 422, "-"),
        ("sombra", 422, "would_fix:alimenta-tu-tiroides-d"),
        ("activo", 200, "fixed:alimenta-tu-tiroides-d"),
    ],
)
def test_a_visit_started_off_the_manifest(
    mode: str | None, status: int, note: str, caplog: pytest.LogCaptureFixture
) -> None:
    app, fake = _app(_settings(_att1_today(mode)))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _om5f_from_org_a())

    assert response.status_code == status
    assert fake is not None and len(fake.calls) == (1 if status == 200 else 0)
    if status == 422:
        assert response.json() == {"detail": "ghl_landing_unknown"}
    record = _one_adapter_record(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage().endswith(f"form_landing={note}")


@pytest.mark.parametrize("mode", ["sombra", "activo"])
def test_a_form_on_its_own_landing_is_an_info_line(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    app, fake = _app(_settings(_att1_today(mode)))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _landing_d())

    assert response.status_code == 200
    record = _one_adapter_record(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage().endswith("form_landing=same")


def test_a_declared_landing_missing_from_the_binding_is_a_warning_in_shadow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Solo con un binding armado a mano (el manifiesto lo rechaza al cargar): en
    # sombra se admite por la URL, y la linea lo dice como warning.
    manifest = _att1_today("sombra")
    config = manifest.to_commercial_ally_config()
    without_b = replace(
        config,
        additional_offer_codes=tuple(c for c in config.additional_offer_codes if c != "bmaztyhg"),
        additional_offer_landings=tuple(
            landing
            for landing in config.additional_offer_landings
            if landing.landing_id != "alimenta-tu-tiroides"
        ),
    )
    app, fake = _app(_settings(manifest, commercial_ally_config=without_b))

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _uhta_from_landing_d())

    assert response.status_code == 200
    assert fake is not None and len(fake.calls) == 1
    record = _one_adapter_record(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage().endswith("form_landing=form_landing_missing:alimenta-tu-tiroides")
