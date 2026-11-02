"""E-283 (P-17): un reintento legitimo de Hotmart no se pierde por la frescura.

Antes, el webhook comparaba ``creation_date`` contra ``HOTMART_MAX_AGE_SECONDS``
(300 s) y respondia ``401 stale_webhook`` antes de persistir. Hotmart reintenta a
los 13, 15 y 30 minutos (log del proxy del 19/09, auditoria de ingreso
``docs/operations/2026-09-19-claude-production-ingress-audit-v1.md``): todo
reintento llegaba viejo y el evento se perdia sin rastro.

Ahora:

* dentro de los 300 s, como siempre;
* del pasado y hasta ``HOTMART_LATE_MAX_AGE_SECONDS`` (72 h): se procesa por el
  camino de siempre y deja ``hotmart_event_late`` en el log. El replay lo frena
  la base: la admision es unica por id y devuelve ``duplicate``;
* mas viejo que eso, o del futuro mas alla de 300 s: ``200 ignored
  stale_webhook`` (antes 401, que Hotmart cuenta como falla del webhook) y una
  linea ``hotmart_event_stale`` con el id, el tipo y la edad.

Datos: la compra real de ATT1 del 26/10
(``tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json``,
anonimizada; llego al bridge 1 s despues de su ``creation_date``) y el carrito
real de ATT1 del 16/10 (``tests/fixtures/hotmart_routing_att1_20261016.json``).
Solo se mueve ``creation_date`` (y el id, para el caso de un id nuevo).
"""

from __future__ import annotations

import copy
import json
import logging
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.hotmart import (
    FRESHNESS_FRESH,
    FRESHNESS_LATE,
    FRESHNESS_STALE,
    HOTMART_LATE_MAX_AGE_SECONDS,
    hotmart_event_freshness,
)
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_TOML = FIXTURES / "instances" / "att1" / "instancia.toml"
PURCHASE = json.loads(
    (FIXTURES / "hotmart_purchase_approved_att1_bienvenida_20261026.json").read_text(
        encoding="utf-8"
    )
)["payload"]
CART = next(
    event["payload"]
    for event in json.loads(
        (FIXTURES / "hotmart_routing_att1_20261016.json").read_text(encoding="utf-8")
    )["eventos"]
    if event["caso"] == "carrito_2uafw5bg"
)
TOKEN = "late-delivery-test-token"
PERSONAL = ("comprador.sintetico@example.com", "+12025550124", "Edith", "Garcia Perez")


def _aged(payload: dict, seconds_ago: float, *, event_id: str | None = None) -> dict:
    event = copy.deepcopy(payload)
    event["creation_date"] = int((time.time() - seconds_ago) * 1000)
    if event_id is not None:
        event["id"] = event_id
    return event


class _Admissions:
    """La unicidad por id de ``webhook_events``: el segundo envio es duplicate."""

    def __init__(self) -> None:
        self.purchases: list[str] = []
        self.carts: list[str] = []
        self._seen: set[str] = set()

    def _outcome(self, event_id: str) -> str:
        if event_id in self._seen:
            return "duplicate"
        self._seen.add(event_id)
        return "inserted"

    async def admit_portable_hotmart_purchase_approved(self, **kwargs: object) -> object:
        event_id = str(kwargs["external_event_id"])
        self.purchases.append(event_id)
        return SimpleNamespace(outcome=self._outcome(event_id), webhook_event_id=event_id)

    async def admit_portable_hotmart_cart_abandonment(self, **kwargs: object) -> object:
        event_id = str(kwargs["external_event_id"])
        self.carts.append(event_id)
        return SimpleNamespace(outcome=self._outcome(event_id), webhook_event_id=event_id)


def _app(tmp_path: Path, supabase: _Admissions) -> object:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["flujos"]["carrito"] = True
    manifest = InstanceManifest.from_mapping(payload)
    settings = Settings(
        webhook_secret="webhook-secret",
        allowed_jid=None,
        capture_dir=tmp_path,
        max_age_seconds=300,
        instance_manifest=manifest,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
        hermes_model_name=manifest.agent_model_name,
        hotmart_hottok=TOKEN,
        hotmart_max_age_seconds=300,
        portable_hotmart_purchase_stop_enabled=True,
        portable_hotmart_recovery_enabled=True,
    )
    return create_app(settings, supabase_client=supabase)  # type: ignore[arg-type]


def _post(app: object, event: dict, *, token: str = TOKEN) -> object:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.post(
            "/webhooks/hotmart",
            content=json.dumps(event),
            headers={"X-HOTMART-HOTTOK": token},
        )


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


# ------------------------------------------------------------ la ventana


def test_the_window_has_three_verdicts() -> None:
    now = 2_000_000_000.0
    at = lambda seconds_ago: int((now - seconds_ago) * 1000)  # noqa: E731

    def verdict(seconds_ago: float) -> str:
        result = hotmart_event_freshness(
            creation_date=at(seconds_ago), max_age_seconds=300, now=now
        )
        assert result is not None
        return result.verdict

    assert verdict(1) == FRESHNESS_FRESH
    assert verdict(300) == FRESHNESS_FRESH
    assert verdict(-300) == FRESHNESS_FRESH
    assert verdict(13 * 60) == FRESHNESS_LATE
    assert verdict(15 * 3600) == FRESHNESS_LATE
    assert verdict(HOTMART_LATE_MAX_AGE_SECONDS) == FRESHNESS_LATE
    assert verdict(HOTMART_LATE_MAX_AGE_SECONDS + 1) == FRESHNESS_STALE
    assert verdict(-301) == FRESHNESS_STALE
    assert hotmart_event_freshness(creation_date=0, max_age_seconds=300).is_stale  # type: ignore[union-attr]
    assert hotmart_event_freshness(creation_date="x", max_age_seconds=300) is None


# ------------------------------------------------------------ el webhook


@pytest.mark.parametrize("minutes_late", [13, 30, 15 * 60])
def test_a_late_retry_of_the_purchase_is_admitted_and_leaves_its_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, minutes_late: int
) -> None:
    supabase = _Admissions()
    app = _app(tmp_path, supabase)

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, _aged(PURCHASE, minutes_late * 60))

    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "received"
    assert supabase.purchases == [PURCHASE["id"]]
    late = [line for line in _lines(caplog) if line.startswith("hotmart_event_late ")]
    assert len(late) == 1, _lines(caplog)
    assert f"event_id={PURCHASE['id']} event_type=PURCHASE_APPROVED" in late[0]
    assert not any(value in line for line in _lines(caplog) for value in PERSONAL)


def test_a_late_cart_is_admitted_too(tmp_path: Path) -> None:
    supabase = _Admissions()
    response = _post(_app(tmp_path, supabase), _aged(CART, 20 * 60))

    assert response.status_code == 202, response.json()
    assert supabase.carts == [CART["id"]]


def test_a_replay_of_the_same_event_is_a_duplicate_at_any_age(tmp_path: Path) -> None:
    supabase = _Admissions()
    app = _app(tmp_path, supabase)
    event = _aged(PURCHASE, 2)

    first = _post(app, event)
    replay = _post(app, event)  # el mismo cuerpo, el mismo creation_date, horas despues

    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate", "event_id": PURCHASE["id"]}


def test_an_event_older_than_the_late_window_is_ignored_with_its_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _Admissions()
    app = _app(tmp_path, supabase)
    event = _aged(PURCHASE, HOTMART_LATE_MAX_AGE_SECONDS + 3600)

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(app, event)

    # 200 y no 401: el hottok es valido, es un descarte deliberado. Un 4xx lo
    # cuenta Hotmart como falla del webhook y, sumadas, lo desactiva.
    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "event_id": PURCHASE["id"],
        "reason": "stale_webhook",
    }
    assert supabase.purchases == []
    stale = [
        record
        for record in caplog.records
        if record.getMessage().startswith("hotmart_event_stale ")
    ]
    assert len(stale) == 1, _lines(caplog)
    assert stale[0].levelno == logging.WARNING
    assert f"event_id={PURCHASE['id']} event_type=PURCHASE_APPROVED" in stale[0].getMessage()
    assert not any(value in line for line in _lines(caplog) for value in PERSONAL)


@pytest.mark.parametrize("creation_date", [0, "future"])
def test_a_zero_or_future_date_is_ignored(tmp_path: Path, creation_date: object) -> None:
    supabase = _Admissions()
    event = copy.deepcopy(PURCHASE)
    event["creation_date"] = (
        0 if creation_date == 0 else int((time.time() + 3600) * 1000)
    )

    response = _post(_app(tmp_path, supabase), event)

    assert response.status_code == 200
    assert response.json()["reason"] == "stale_webhook"
    assert supabase.purchases == []


def test_a_wrong_token_is_still_401_before_anything(tmp_path: Path) -> None:
    supabase = _Admissions()
    response = _post(_app(tmp_path, supabase), _aged(PURCHASE, 20 * 60), token="otro")

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_token"}
    assert supabase.purchases == []
