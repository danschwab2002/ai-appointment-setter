"""E-668: el comando del operador valida con lo que ejecuta el bridge vivo.

El defecto (Dan, 2026-11-16 ≈14:05Z, ``operador-de-flujos.sh publicar ... --sin-simulacion`` con las propuestas
de la prueba A de Maribel): la validacion propia del operador daba ``motor_nativo:disparador``,
``motor_nativo:permiso`` y ``motor_nativo:forma`` sobre ``/dispara`` y ``/entra/permiso`` de un flujo de
audiencia, y no publicaba. El validador corre en un contenedor nuevo de la imagen (V2#4), y el comando no le
pasaba ``instance.runtime``: sin el, el motor nativo es «apagado» (``InstanceRuntime`` ausente), y un disparo por
audiencia necesita ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED``. La ruta del AE y la pagina validan con
``instance_runtime_from(settings)``.

Ahora (D2) el bridge publica ese mismo runtime en /ready (``flow_change_runtime``, JSON de
``InstanceRuntime.to_json``: flags y el producto principal) y el operador, con ``CONTENEDOR_BRIDGE``, lo lee
desde adentro del contenedor y se lo pasa al validador. Sin la variable, como antes. Con la variable y sin
poder leerlo, no valida (D3).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from test_e668_audience_version_simulation import FIXTURE
from test_flow_change_operator_script import ENV, FakeDocker, FakeTerminal
from test_flow_change_operator_script import script as operator_script
from test_flow_engine_ready import _app, _ready, _ReadyAuthority

from bridge.flow_change_operator import instance_from_input
from bridge.flow_validator import NativeRuntime

RUNTIME = FIXTURE["runtime"]


class BridgeDocker(FakeDocker):
    """El VPS con el bridge vivo: ``docker exec <bridge> python -c <sonda>`` contesta su /ready recortado."""

    def __init__(self, runtime: object = RUNTIME, *, exit_code: int = 0, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.runtime = runtime

        self.exit_code = exit_code

    def run(self, argv, stdin=""):
        argv = list(argv)
        if argv[:1] == ["exec"] and "python" in argv:
            self.calls.append((argv, stdin))
            raw = self.runtime if isinstance(self.runtime, str) or self.runtime is None else json.dumps(self.runtime)
            return subprocess.CompletedProcess(argv, self.exit_code, json.dumps({"flow_change_runtime": raw}) + "\n", "")
        return super().run(argv, stdin)


def _ops(docker: FakeDocker, **env: str) -> operator_script.Ops:
    return operator_script.Ops(operator_script.Config.from_env({**ENV, **env}), docker=docker,
                               terminal=FakeTerminal(), out=io.StringIO(), code=lambda: "abc123")


def _module_payloads(docker: FakeDocker) -> list[dict]:
    return [json.loads(stdin) for argv, stdin in docker.calls if argv[:1] == ["run"]]


def test_with_the_bridge_container_the_validator_gets_its_runtime() -> None:
    docker = BridgeDocker()
    ops = _ops(docker, CONTENEDOR_BRIDGE="maribel_appointment-bridge")
    world = ops.read_world()
    ops.validate({"id": "x", "revision": 1, "items": [], "draft_base": {}, "purpose": "p",
                  "requested_by_kind": "operador"}, world, None)
    [payload] = _module_payloads(docker)
    assert payload["instance"]["runtime"] == RUNTIME
    [(argv, _)] = [(argv, stdin) for argv, stdin in docker.calls if argv[:1] == ["exec"] and "python" in argv]
    # Desde adentro del contenedor del bridge (filtrado a uno), sin shell: /ready no esta publicado afuera.
    assert argv[:5] == ["exec", "-i", "maribel_appointment-bridge-0", "python", "-c"]
    assert "127.0.0.1:8000/ready" in argv[5] and "flow_change_runtime" in argv[5]
    # El validador del bridge lo entiende como el de la ruta del AE.
    runtime = instance_from_input(payload["instance"]).runtime
    assert isinstance(runtime, NativeRuntime) and runtime.native_multitouch is True
    # Se lee una vez por comando.
    ops.validate({"id": "x", "revision": 1, "items": [], "draft_base": {}, "purpose": "p",
                  "requested_by_kind": "operador"}, world, None)
    assert len([argv for argv, _ in docker.calls if argv[:1] == ["exec"] and "python" in argv]) == 1


def test_without_the_variable_nothing_changes() -> None:
    docker = BridgeDocker()
    ops = _ops(docker)
    ops.validate({"id": "x", "revision": 1, "items": [], "draft_base": {}, "purpose": "p",
                  "requested_by_kind": "operador"}, ops.read_world(), None)
    [payload] = _module_payloads(docker)
    assert "runtime" not in payload["instance"]
    assert not [argv for argv, _ in docker.calls if argv[:1] == ["exec"] and "python" in argv]


@pytest.mark.parametrize("runtime", [
    None,                                   # un bridge sin E-668: /ready no trae la clave
    "no es json",
    {**RUNTIME, "native_multitouch_enabled": "true"},
    {**RUNTIME, "otra": True},
    {key: value for key, value in RUNTIME.items() if key != "main_product"},
    {**RUNTIME, "main_product": "Maribel; rm"},
])
def test_a_runtime_that_does_not_read_refuses_to_validate(runtime: object) -> None:
    ops = _ops(BridgeDocker(runtime), CONTENEDOR_BRIDGE="maribel_appointment-bridge")
    with pytest.raises(operator_script.Refused, match="flow_change_runtime"):
        ops.read_runtime()


def test_a_bridge_that_does_not_answer_refuses_to_validate() -> None:
    ops = _ops(BridgeDocker(exit_code=1), CONTENEDOR_BRIDGE="maribel_appointment-bridge")
    with pytest.raises(operator_script.Refused, match="flow_change_runtime"):
        ops.read_runtime()


def test_the_bridge_publishes_its_runtime_in_ready(tmp_path: Path) -> None:
    from test_flow_engine_bridge_settings import engine_settings

    from bridge.app import instance_runtime_from

    payload = _ready(_app(tmp_path, _ReadyAuthority())).json()
    published = json.loads(payload["flow_change_runtime"])
    settings = engine_settings(tmp_path)
    expected = instance_runtime_from(settings, native_silence_sweeper=published["native_silence_sweeper"])
    assert published == expected.to_json()
    # Lo que lee el operador: la misma forma que acepta su validacion.
    assert operator_script._runtime_ok(published)  # noqa: SLF001
