"""Las lineas info revisadas del bridge llegan al log del contenedor (E-084).

En produccion el bridge corre bajo uvicorn, que configura solo sus loggers: el
raiz queda sin handler y un ``logger.info`` del bridge se perdia. Por eso
``hotmart_event_filtered`` existia en el codigo y no en el log (E-081).

La prueba de punta a punta corre en un interprete nuevo, con la configuracion
de logging de uvicorn (``uvicorn.Config`` la aplica al crearse, como en el
arranque del contenedor) y sin los handlers que pytest pone en el raiz. Lee la
salida de error del proceso, que es lo que muestra ``docker service logs``.

Fixtures capturados:
- el pago rechazado: tests/fixtures/hotmart_purchase_canceled_att1_20261019.json
  (att1-db, el 2026-10-19, anonimizado por lista blanca);
- el carrito: tests/fixtures/hotmart_routing_att1_20261016.json (att1-db, el
  2026-10-16, comprador de prueba).
El tipo no soportado sale del pago rechazado capturado con ``event`` cambiado a
``PURCHASE_COMPLETE``, un tipo que Hotmart manda y el bridge no usa.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bridge import event_log
from bridge.event_log import LOGGED_INFO_EVENTS, configure_bridge_event_log

ROOT = Path(__file__).resolve().parents[1]

# Lo que no puede salir en ninguna linea: el comprador de los fixtures.
PERSONAL = (
    "comprador.prueba@example.test",
    "5215550000001",
    "persona.prueba1@example.com",
    "520000000001",
    "Comprador Prueba",
    "Persona Prueba",
)

_CHILD = r'''
import asyncio, copy, json, logging, sys, time, tomllib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi.testclient import TestClient

# El arranque del contenedor: uvicorn bridge.app:build_app --factory. Config
# aplica LOGGING_CONFIG de uvicorn al crearse; el raiz queda sin handlers.
uvicorn.Config("bridge.app:build_app", factory=True)
assert not logging.getLogger().handlers

from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(sys.argv[1])
CASE = sys.argv[2]
TOKEN = "event-log-test-token"


def fresh(payload):
    payload = copy.deepcopy(payload)
    payload["creation_date"] = int(time.time() * 1000)
    return payload


canceled = json.loads(
    (FIXTURES / "hotmart_purchase_canceled_att1_20261019.json").read_text("utf-8")
)["eventos"][0]["payload"]
cart = next(
    e["payload"]
    for e in json.loads(
        (FIXTURES / "hotmart_routing_att1_20261016.json").read_text("utf-8")
    )["eventos"]
    if e["caso"] == "carrito_2uafw5bg"
)
manifest_payload = tomllib.loads(
    (FIXTURES / "instances" / "att1" / "instancia.toml").read_text("utf-8")
)
manifest = InstanceManifest.from_mapping(manifest_payload)
settings = Settings(
    webhook_secret="webhook-secret",
    allowed_jid=None,
    capture_dir=Path("/tmp/event-log-tests"),
    max_age_seconds=300,
    instance_manifest=manifest,
    commercial_ally_config=manifest.to_commercial_ally_config(),
    commercial_ally_manifest_path=Path("/runtime/instancia.toml"),
    hermes_model_name=manifest.agent_model_name,
    hotmart_hottok=TOKEN,
    portable_hotmart_purchase_stop_enabled=True,
)


class Admissions:
    async def admit_portable_hotmart_listen_only_capture(self, **kwargs):
        raise AssertionError("no capture expected")


app = create_app(settings, supabase_client=Admissions())

if CASE == "filtered":
    events = [fresh(canceled), fresh(cart)]
elif CASE == "unsupported":
    event = fresh(canceled)
    event["event"] = "PURCHASE_COMPLETE"
    events = [event]
elif CASE == "odd_type":
    event = fresh(canceled)
    event["event"] = "comprador.prueba@example.test"
    events = [event]
else:
    events = []

with TestClient(app) as client:
    for event in events:
        response = client.post(
            "/webhooks/hotmart",
            content=json.dumps(event),
            headers={"X-HOTMART-HOTTOK": TOKEN},
        )
        print(json.dumps({"status": response.status_code, "id": event["id"],
                          "body": response.json()}))

if CASE == "levels":
    probe = logging.getLogger("bridge.app")
    probe.warning("event_log_probe_warning n=1")
    probe.info("greeting_name name=%s", "Comprador Prueba")
    probe.info("hotmart_event_filtered event_id=probe event_type=PROBE reason=x")
    probe.debug("hotmart_event_filtered event_id=debug")
    logging.getLogger("bridge.worker").info(
        "hotmart_purchase_processed event_id=probe outcome=updated"
    )
    # Fuera del bridge todo sigue como antes: el warning sale, el info no.
    logging.getLogger("httpx").info("HTTP Request: GET http://x/?phone=5215550000001")
    logging.getLogger("httpx").warning("httpx_probe_warning")
'''


def _run(case: str) -> tuple[list[dict], list[str]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(ROOT / "tests" / "fixtures"), case],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines() if line]
    return responses, result.stderr.splitlines()


def _assert_no_personal(lines: list[str]) -> None:
    assert not any(value in line for line in lines for value in PERSONAL), lines


def test_a_filtered_event_leaves_one_line_in_the_container_log() -> None:
    responses, lines = _run("filtered")

    assert [r["body"]["reason"] for r in responses] == [
        "portable_purchase_stop_event_ignored",
        "portable_purchase_stop_event_ignored",
    ]
    for response in responses:
        matching = [
            line
            for line in lines
            if line.startswith("hotmart_event_filtered ")
            and f"event_id={response['id']}" in line
        ]
        assert len(matching) == 1, lines
    assert any("event_type=PURCHASE_CANCELED" in line for line in lines), lines
    assert any("event_type=PURCHASE_OUT_OF_SHOPPING_CART" in line for line in lines)
    _assert_no_personal(lines)


def test_an_unsupported_event_type_leaves_its_line() -> None:
    responses, lines = _run("unsupported")

    assert responses[0]["body"] == {
        "status": "ignored",
        "reason": "unsupported_event_type",
    }
    assert lines.count(
        f"hotmart_event_ignored event_id={responses[0]['id']} "
        "event_type=PURCHASE_COMPLETE reason=unsupported_event_type"
    ) == 1, lines
    _assert_no_personal(lines)


def test_free_text_in_the_event_type_never_reaches_the_log() -> None:
    responses, lines = _run("odd_type")

    assert responses[0]["body"]["reason"] == "unsupported_event_type"
    assert any(
        line.startswith("hotmart_event_ignored ") and "event_type=<otro>" in line
        for line in lines
    ), lines
    _assert_no_personal(lines)


def test_only_reviewed_info_lines_go_out_and_warnings_go_out_once() -> None:
    _, lines = _run("levels")

    assert lines.count("event_log_probe_warning n=1") == 1, lines
    assert lines.count(
        "hotmart_event_filtered event_id=probe event_type=PROBE reason=x"
    ) == 1, lines
    assert lines.count("hotmart_purchase_processed event_id=probe outcome=updated") == 1
    assert not any(line.startswith("greeting_name") for line in lines), lines
    assert not any("event_id=debug" in line for line in lines), lines
    assert not any(line.startswith("HTTP Request") for line in lines), lines
    assert lines.count("httpx_probe_warning") == 1, lines
    _assert_no_personal(lines)


# ----------------------------------------------------------- en el proceso


@pytest.fixture
def bare_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """El logger bridge como lo deja uvicorn. El raiz se vacia en cada test:
    pytest le suma su handler recien al empezar la fase de la llamada."""
    bridge_logger = logging.getLogger(event_log.BRIDGE_LOGGER)
    previous_level = bridge_logger.level
    monkeypatch.setattr(bridge_logger, "handlers", [])
    bridge_logger.setLevel(logging.NOTSET)
    yield bridge_logger
    bridge_logger.setLevel(previous_level)


def test_configure_is_idempotent(
    bare_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    stream = io.StringIO()

    assert configure_bridge_event_log(stream) is True
    assert configure_bridge_event_log(stream) is False

    logging.getLogger("bridge.app").info(
        "hotmart_event_unrouted event_id=a event_type=B product_id=1 offer=- reason=r"
    )
    assert stream.getvalue().splitlines() == [
        "hotmart_event_unrouted event_id=a event_type=B product_id=1 offer=- reason=r"
    ]


def test_nothing_is_installed_when_the_root_already_has_handlers(
    bare_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.NullHandler()])

    assert configure_bridge_event_log(io.StringIO()) is False
    assert bare_logging.handlers == []


def test_every_reviewed_event_is_a_hotmart_line_present_in_the_code() -> None:
    sources = "\n".join(
        (ROOT / "src" / "bridge" / name).read_text(encoding="utf-8")
        for name in ("app.py", "worker.py")
    )
    for name in LOGGED_INFO_EVENTS:
        assert name.startswith("hotmart_"), name
        assert f'"{name} ' in sources, name
