"""El script del VPS del comando del operador (scripts/flow_change_operator.py), con un docker falso.

Lo que se prueba aca es lo que el script decide antes de llamar a la base: a
quien deja escribir, que no escribe sin el codigo leido de la terminal, que la
pausa manda la generacion que leyo, que los valores van en el bloque COPY y
nunca en el texto del SQL, y que el validador corre en un contenedor nuevo,
sin red y desde la imagen fijada. Lo que hace la base con cada llamada lo
prueba, contra un PostgreSQL real con la cadena aplicada,
tests/sql/followup_engine/real_postgres_flow_change_operator.py.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flow_change_operator_script", ROOT / "scripts/flow_change_operator.py")
assert SPEC and SPEC.loader
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

IMAGE = "setter-bridge@sha256:" + "d" * 64
ENV = {"CONTENEDOR_DB": "att1-db", "CONTENEDOR_CHATWOOT_DB": "chatwoot-db", "BASE_CHATWOOT_DB": "infra",
       "BANDEJA_CHATWOOT": "11", "IMAGEN_BRIDGE": IMAGE, "TENANT": "lancemos", "ALIADA": "att1",
       "ZONA": "America/Mexico_City", "APROBADORES": "dan"}
CART = "00000001-0000-4000-8000-0000000a7701"
PROPOSAL = "11111111-0000-4000-8000-000000000001"
FINGERPRINT = "f" * 64
FLOWS = [{"flow_id": CART, "product_ref": "att1", "flow_key": "carrito", "state": "prendido", "active_version": 1,
          "generation": 7, "changed_at": "2026-10-21T15:00:00Z", "changed_by_kind": "operador",
          "versions": {"1": {"esquema": "setter-flujo/v1", "pasos": [{"plantilla": "att1_carrito_abandonado_01"}]}}}]
PURPOSE = "que el carrito\tespere 2 horas\ny nada \\mas"


def proposal(status: str = "validada", kind: str = "automation_expert") -> dict:
    return {"id": PROPOSAL, "status": status, "effective_status": status, "revision": 1,
            "items": [{"producto": "att1", "flujo": "carrito", "flow_id": CART, "tipo": "version",
                       "definicion": {"esquema": "setter-flujo/v1",
                                      "pasos": [{"esperar": "2h"}, {"plantilla": "att1_carrito_abandonado_01"}]},
                       "en_curso": "seguir"}],
            "draft_base": {CART: {"active_version": 1}}, "purpose": PURPOSE, "requested_by_kind": kind,
            "fingerprint": FINGERPRINT, "validated_at": None, "expires_at": "2026-10-22T15:00:00Z",
            "created_at": "2026-10-21T15:00:00Z", "approved_by_ref": None, "validation": None}


def validation(*, gaps: tuple[str, ...] = ("simulation_not_built",), blocks: int = 0) -> dict:
    return {"ok": blocks == 0, "blocks": blocks, "catalog_sha256": "c" * 64, "validated_at": "x",
            "validated_base": {CART: {"active_version": 1, "generation": 7}},
            "validator": {"contract": "setter-flujo/v1", "rules_version": 1, "phase": "publish",
                          "actor": "automation_expert", "product_version": "1.15.5", "git_sha": "b163669"},
            "result": {"blocks": [{"code": "x", "path": "/0"}] * blocks, "warnings": []},
            "preview": {"huecos": [{"code": code} for code in gaps]}}


class FakeDocker:
    def __init__(self, *, containers: int = 1, prop: dict | None = None, valid: dict | None = None,
                 fail_with: str | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.containers = containers
        self.prop = proposal() if prop is None else prop
        self.valid = validation() if valid is None else valid
        self.fail_with = fail_with

    def writes(self) -> list[str]:
        return [stdin for argv, stdin in self.calls if argv[:1] == ["exec"] and "select public.operator_" in stdin]

    def run(self, argv, stdin=""):
        argv = list(argv)
        self.calls.append((argv, stdin))
        ok = lambda out: subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if argv[0] == "ps":
            return ok("".join(f"{argv[2][5:]}-{i}\n" for i in range(self.containers)))
        if argv[0] == "run":
            return subprocess.CompletedProcess(argv, 0 if self.valid["ok"] else 3, json.dumps(self.valid), "")
        if self.fail_with and "select public.operator_" in stdin:
            return subprocess.CompletedProcess(argv, 3, "", f"psql:<stdin>:9: ERROR:  {self.fail_with}\nDETAIL: x")
        if "commercial_ally_runtime_bindings" in stdin:
            return ok('["att1"]\n' + json.dumps(FLOWS) + "\n")
        if "from public.flow_change_proposals p\n where p.id" in stdin:
            return ok(json.dumps(self.prop) + "\n")
        if "channel_whatsapp" in stdin:
            return ok("[]\n")
        if "operator_pause_flow_v1" in stdin:
            return ok('{"ok": true, "changed": true}\n')
        return ok('{"ok": true, "replayed": false, "status": "aprobada"}\n')


class FakeTerminal:
    def __init__(self, answer: str | None = "right") -> None:
        self.answer = answer
        self.asked = 0

    def ask(self, prompt: str) -> str:
        self.asked += 1
        if self.answer is None:
            raise script.Refused("sin terminal no se confirma: este comando lo corre una persona")
        return "abc123" if self.answer == "right" else self.answer


def run(argv: list[str], docker: FakeDocker, terminal: FakeTerminal | None = None, env: dict | None = None):
    ops = script.Ops(script.Config.from_env(ENV if env is None else env), docker=docker,
                     terminal=terminal or FakeTerminal(), out=io.StringIO(), code=lambda: "abc123")
    code = script.main(argv, ops=ops)
    return code, ops.out.getvalue()


def test_someone_who_is_not_an_approver_does_not_reach_docker() -> None:
    docker = FakeDocker()
    code, out = run(["publicar", PROPOSAL, "--quien", "brayan", "--sin-simulacion"], docker)
    assert code == script.EXIT_USAGE and "APROBADORES" in out
    assert docker.calls == []


def test_without_a_terminal_nothing_is_written() -> None:
    docker = FakeDocker()
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker, FakeTerminal(None))
    assert code == script.EXIT_REFUSED and "sin terminal" in out
    assert docker.writes() == []


def test_a_wrong_code_writes_nothing() -> None:
    docker = FakeDocker()
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker, FakeTerminal("zzz"))
    assert code == script.EXIT_REFUSED and "no coincide" in out
    assert docker.writes() == []


def test_without_the_simulation_it_refuses_unless_asked_and_before_the_code() -> None:
    docker, terminal = FakeDocker(), FakeTerminal()
    code, out = run(["publicar", PROPOSAL, "--quien", "dan"], docker, terminal)
    assert code == script.EXIT_REFUSED and "--sin-simulacion" in out
    assert docker.writes() == [] and terminal.asked == 0


def test_with_blocks_now_it_does_not_publish() -> None:
    docker = FakeDocker(valid=validation(blocks=1))
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    assert code == script.EXIT_BLOCKED and docker.writes() == []


def test_publish_approves_then_publishes_with_the_fingerprint_of_the_proposal() -> None:
    docker = FakeDocker()
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    assert code == script.EXIT_OK, out
    approve, publish = docker.writes()
    assert "operator_approve_flow_change_v1" in approve and "operator_publish_flow_change_v1" in publish
    revalidation = json.loads(_copy_value(publish, "revalidation"))
    assert set(revalidation) == {"catalog_sha256", "fingerprint", "result", "validated_at", "validator"}
    assert revalidation["fingerprint"] == FINGERPRINT


def test_an_approved_proposal_is_published_without_approving_again() -> None:
    docker = FakeDocker(prop=proposal("aprobada"))
    code, _ = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    assert code == script.EXIT_OK
    [publish] = docker.writes()
    assert "operator_publish_flow_change_v1" in publish


def test_the_diff_is_computed_by_the_script_and_the_agent_text_is_fenced_and_escaped() -> None:
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], FakeDocker())
    assert '"/pasos/0/esperar": null -> "2h"' in out
    lines = out.splitlines()
    start = lines.index(script.AGENT_TEXT_OPEN)
    assert lines[start + 1] == json.dumps(PURPOSE) and lines[start + 2] == script.AGENT_TEXT_CLOSE


def test_values_go_in_the_copy_block_never_in_the_sql_text() -> None:
    docker = FakeDocker()
    run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    for argv, stdin in docker.calls:
        if argv[:1] != ["exec"]:
            continue
        assert argv[argv.index("psql") + 1] == "-X"
        sql = stdin.split("\n\\.\n", 1)[1]
        for value in (PROPOSAL, FINGERPRINT, "lancemos", "dan"):
            assert value not in sql
    # el texto con tabulador, salto de linea y barra viaja escapado y vuelve igual.
    assert script._copy_escape(PURPOSE) == "que el carrito\\tespere 2 horas\\ny nada \\\\mas"


def test_the_validator_runs_in_a_fresh_container_without_network_from_the_pinned_image() -> None:
    docker = FakeDocker()
    run(["ver", PROPOSAL], docker)
    [(argv, _)] = [call for call in docker.calls if call[0][0] == "run"]
    assert argv[:2] == ["run", "--rm"] and "--read-only" in argv
    assert argv[argv.index("--network") + 1] == "none" and IMAGE in argv
    assert argv[-3:] == ["-m", "bridge.flow_change_operator", "validar"]
    assert not [call for call in docker.calls if call[0][:2] == ["exec", "-i"] and "bridge" in call[0][2]]


def test_pause_sends_the_generation_it_read_before_confirming() -> None:
    docker = FakeDocker()
    code, out = run(["pausar", "att1/carrito", "--quien", "dan", "--motivo", "pausa"], docker)
    assert code == script.EXIT_OK, out
    [pause] = docker.writes()
    assert "operator_pause_flow_v1" in pause and _copy_value(pause, "generation") == "7"


def test_validar_does_not_record_a_proposal_of_the_agent() -> None:
    docker = FakeDocker(prop=proposal("borrador", kind="automation_expert"))
    code, out = run(["validar", PROPOSAL, "--quien", "dan"], docker)
    assert code == script.EXIT_REFUSED and "ruta del bridge" in out and docker.writes() == []


def test_a_base_error_shows_only_its_code() -> None:
    docker = FakeDocker(fail_with="flow_change_stale_base")
    code, out = run(["publicar", PROPOSAL, "--quien", "dan", "--sin-simulacion"], docker)
    assert code == script.EXIT_REFUSED and "flow_change_stale_base" in out and "DETAIL" not in out


def test_a_filter_with_two_containers_is_refused() -> None:
    code, out = run(["estado"], FakeDocker(containers=2))
    assert code == script.EXIT_REFUSED and "exactamente un contenedor" in out


@pytest.mark.parametrize(("name", "value"), [("IMAGEN_BRIDGE", "setter-bridge:v1.15.5"), ("APROBADORES", ""),
                                             ("BANDEJA_CHATWOOT", "once")])
def test_the_configuration_is_checked_before_anything_runs(name: str, value: str) -> None:
    with pytest.raises(script.Refused):
        script.Config.from_env({**ENV, name: value})


def test_estado_opens_with_the_line_that_the_store_does_not_govern_yet() -> None:
    code, out = run(["estado"], FakeDocker())
    assert code == script.EXIT_OK and out.splitlines()[0] == script.NOT_GOVERNING
    assert "att1/carrito: prendido, version 1, generacion 7" in out


def test_volver_to_the_active_version_is_refused() -> None:
    docker = FakeDocker()
    code, out = run(["volver", "att1/carrito", "1", "--quien", "dan", "--motivo", "x"], docker)
    assert code == script.EXIT_REFUSED and docker.writes() == []


def _copy_value(stdin: str, key: str) -> str:
    block = stdin.split("copy pg_temp._args (k, v) from stdin;\n", 1)[1].split("\n\\.\n", 1)[0]
    for line in block.splitlines():
        found_key, value = line.split("\t", 1)
        if found_key == key:
            return value.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
    raise AssertionError(f"{key} is not in the COPY block")
