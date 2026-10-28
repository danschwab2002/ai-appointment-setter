"""``mover`` del comando del operador: la ruta de un flujo entre ``legado`` y ``motor`` (E-020, P1 de la T5).

El script corre con un docker falso, y el contenedor del validador corre de
verdad: el falso llama en el proceso a ``bridge.flow_route_revalidation`` con
la entrada que el script le manda. El almacen es la lectura real de ATT1 del
22/10 (``tests/fixtures/flow_change/att1_read_flow_store_v1_20261022.json``,
sesion de solo lectura, sin datos personales) y el catalogo, el de la bandeja
11 del 21/10 (``tests/fixtures/flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json``).
Lo que hace la base con la llamada (el CAS, la revalidacion, el scope armado)
lo prueba contra la cadena real ``tests/sql/followup_engine/validate_flow_engine_motor_v1.mjs``.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

from bridge import flow_route_revalidation

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flow_change_operator_script_mover", ROOT / "scripts/flow_change_operator.py")
assert SPEC and SPEC.loader
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

STORE = json.loads((ROOT / "tests/fixtures/flow_change/att1_read_flow_store_v1_20261022.json").read_text())["response"]
CATALOG = json.loads((ROOT / "tests/fixtures/flow_change_operator/"
                      "chatwoot_db_message_templates_inbox_11_20261021.json").read_text())["message_templates"]
CART = next(flow for flow in STORE["flows"] if flow["flow_key"] == "carrito")
SHA = "a" * 64
IMAGE = "setter-bridge@sha256:" + "d" * 64
ENV = {"CONTENEDOR_DB": "att1-db", "CONTENEDOR_CHATWOOT_DB": "chatwoot-db", "BASE_CHATWOOT_DB": "infra",
       "BANDEJA_CHATWOOT": "11", "IMAGEN_BRIDGE": IMAGE, "TENANT": "lancemos", "ALIADA": "att1",
       "ZONA": "America/Mexico_City", "APROBADORES": "dan"}
IMAGE_ENV = {"SETTER_VERSION": "1.16.3", "GIT_SHA": "b4d6027"}


def route_row(executor: str = "legado", generation: int = 0, **changes: object) -> dict:
    row = {"executor": executor, "generation": generation, "legacy_kind": "carrito",
           "legacy_scope_key": "att1-recuperacion", "state": CART["state"], "active_version": CART["active_version"],
           "definition_sha256": SHA, "legacy_runtime_state": "armed", "legacy_scope_version": 7,
           "kind_supported": True, "person_cap_present": False,
           "en_curso_legado": 67, "en_curso_motor": 0}
    row.update(changes)
    return row


class FakeDocker:
    def __init__(self, route: dict | None = None, flows: list | None = None, answer: dict | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.answer = answer
        self.route = route_row() if route is None else route
        self.flows = copy.deepcopy(STORE["flows"]) if flows is None else flows

    def writes(self) -> list[str]:
        return [stdin for argv, stdin in self.calls if argv[:1] == ["exec"] and "select public.operator_" in stdin]

    def module_runs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[:1] == ["run"]]

    def run(self, argv, stdin=""):
        argv = list(argv)
        self.calls.append((argv, stdin))
        ok = lambda out: subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if argv[0] == "ps":
            return ok(f"{argv[2][5:]}-0\n")
        if argv[0] == "run":
            # El contenedor nuevo de la imagen: el modulo de verdad, con la identidad de la imagen.
            assert argv[-2:] == ["bridge.flow_route_revalidation", "revalidar"], argv
            out = io.StringIO()
            code = flow_route_revalidation.main(["revalidar"], stdin=io.StringIO(stdin), stdout=out, environ=IMAGE_ENV)
            return subprocess.CompletedProcess(argv, code, out.getvalue(), "")
        if "operator_route_flow_v1" in stdin:
            if self.answer is not None:
                return ok(json.dumps(self.answer) + "\n")
            executor = _copy_value(stdin, "executor")
            route = {**self.route, "executor": executor, "generation": self.route["generation"] + 1}
            return ok(json.dumps({"ok": True, "route": route}) + "\n")
        if "from public.flow_engine_routes r join public.flow_controls c on c.flow_id = r.flow_id\n where" in stdin \
                and "'en_curso_legado'" in stdin:
            return ok(json.dumps(self.route) + "\n")
        if "from public.flow_engine_routes r" in stdin:
            return ok("[]\n")
        if "commercial_ally_runtime_bindings" in stdin:
            return ok('["att1"]\n' + json.dumps(self.flows) + "\n")
        if "channel_whatsapp" in stdin:
            return ok(json.dumps(CATALOG) + "\n")
        raise AssertionError(f"consulta inesperada: {stdin[-200:]}")


class FakeTerminal:
    def __init__(self, answer: str | None = "right") -> None:
        self.answer = answer
        self.asked = 0

    def ask(self, prompt: str) -> str:
        self.asked += 1
        if self.answer is None:
            raise script.Refused("sin terminal no se confirma: este comando lo corre una persona")
        return "abc123" if self.answer == "right" else self.answer


def run(argv: list[str], docker: FakeDocker, terminal: FakeTerminal | None = None):
    ops = script.Ops(script.Config.from_env(ENV), docker=docker, terminal=terminal or FakeTerminal(),
                     out=io.StringIO(), code=lambda: "abc123")
    code = script.main(argv, ops=ops)
    return code, ops.out.getvalue()


def _copy_value(stdin: str, key: str) -> str:
    block = stdin.split("copy pg_temp._args (k, v) from stdin;\n", 1)[1].split("\n\\.\n", 1)[0]
    for line in block.splitlines():
        found_key, value = line.split("\t", 1)
        if found_key == key:
            return value.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
    raise AssertionError(f"{key} is not in the COPY block")


MOVE = ["mover", "att1/carrito", "motor", "--quien", "dan", "--motivo", "T5: el carrito pasa al motor en la v1"]
BACK = ["mover", "att1/carrito", "legado", "--quien", "dan", "--motivo", "T5: vuelta"]


def test_to_motor_revalidates_the_active_version_with_the_image_and_moves_with_the_generation_read() -> None:
    docker, terminal = FakeDocker(), FakeTerminal()
    code, out = run(MOVE, docker, terminal)
    assert code == script.EXIT_OK, out
    assert terminal.asked == 1
    # E-165: el bridge sin A1 ni A2/Z manda el carrito al legado; el operador lo mira en /ready.
    assert "flow_engine_cart_touches: enabled" in out
    assert out.index("flow_engine_cart_touches") < out.index("despues (la ruta ya se movio")
    [write] = docker.writes()
    assert "select public.operator_route_flow_v1(" in write
    assert (_copy_value(write, "executor"), _copy_value(write, "generation")) == ("motor", "0")
    assert (_copy_value(write, "product"), _copy_value(write, "flow")) == ("att1", "carrito")
    revalidation = json.loads(_copy_value(write, "revalidation"))
    # Las seis claves exactas de _flow_engine_revalidation_is_valid (20261021000200:339).
    assert set(revalidation) == {"version", "definition_sha256", "rules_version", "capabilities", "blocks",
                                 "validator"}
    assert revalidation["version"] == CART["active_version"] and revalidation["definition_sha256"] == SHA
    assert revalidation["rules_version"] >= 2 and revalidation["capabilities"] == "v1"
    assert revalidation["blocks"] == []
    assert revalidation["validator"] == {"contract": "setter-flujo/v1", "rules_version": 1, "phase": "publish",
                                         "actor": "operador", "product_version": "1.16.3", "git_sha": "b4d6027"}
    # El validador corre en un contenedor nuevo de la imagen fijada, sin red, como en publicar.
    [argv] = docker.module_runs()
    assert IMAGE in argv and ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
    assert 'en_curso: {"legado": 67, "motor": 0}' in out and "Lo en curso no se mueve" in out


def test_the_way_back_to_legado_goes_without_revalidation_and_asks_for_the_code() -> None:
    docker, terminal = FakeDocker(route_row("motor", 1)), FakeTerminal()
    code, out = run(BACK, docker, terminal)
    assert code == script.EXIT_OK, out
    [write] = docker.writes()
    assert (_copy_value(write, "executor"), _copy_value(write, "generation")) == ("legado", "1")
    assert _copy_value(write, "revalidation") == "\\N"
    assert docker.module_runs() == [] and terminal.asked == 1


def test_without_a_terminal_or_with_a_wrong_code_nothing_is_written() -> None:
    for answer, message in ((None, "sin terminal"), ("zzz", "no coincide")):
        for argv, route in ((MOVE, route_row()), (BACK, route_row("motor", 1))):
            docker = FakeDocker(route)
            code, out = run(argv, docker, FakeTerminal(answer))
            assert code == script.EXIT_REFUSED and message in out
            assert docker.writes() == []


def test_blocks_with_the_engine_stop_it_before_the_code() -> None:
    flows = copy.deepcopy(STORE["flows"])
    cart = next(flow for flow in flows if flow["flow_key"] == "carrito")
    # Una plantilla que no esta en el catalogo de la bandeja (regla 13).
    cart["versions"]["1"]["pasos"][2]["plantilla"] = "att1_plantilla_que_no_existe"
    docker, terminal = FakeDocker(flows=flows), FakeTerminal()
    code, out = run(MOVE, docker, terminal)
    assert code == script.EXIT_BLOCKED and "bloqueos" in out
    assert docker.writes() == [] and terminal.asked == 0


def test_what_the_base_would_refuse_is_refused_before_the_code() -> None:
    cases = [
        (MOVE, route_row(person_cap_present=True), "tope por persona"),
        (BACK, route_row("motor", 1, legacy_runtime_state="paused"), "no esta armado"),
        (MOVE, route_row(active_version=2), "cambio entre las dos lecturas"),
        (MOVE, route_row(kind_supported=False), "no ejecuta carrito"),
    ]
    for argv, route, message in cases:
        docker, terminal = FakeDocker(route), FakeTerminal()
        code, out = run(argv, docker, terminal)
        assert code == script.EXIT_REFUSED and message in out, out
        assert docker.writes() == [] and terminal.asked == 0


def test_the_same_executor_writes_nothing_and_a_flow_without_route_is_refused() -> None:
    docker, terminal = FakeDocker(route_row("motor", 1)), FakeTerminal()
    code, out = run(MOVE, docker, terminal)
    assert code == script.EXIT_OK and "ya esta en motor" in out
    assert docker.writes() == [] and terminal.asked == 0
    docker = FakeDocker()
    docker.route = None  # type: ignore[assignment]
    code, out = run(MOVE, docker)
    assert code == script.EXIT_REFUSED and "no tiene ruta" in out and docker.writes() == []


def test_a_wrong_destination_or_someone_not_approver_does_not_reach_the_base() -> None:
    docker = FakeDocker()
    code, out = run(["mover", "att1/carrito", "motores", "--quien", "dan", "--motivo", "x"], docker)
    assert code == script.EXIT_USAGE and docker.calls == []
    code, out = run(["mover", "att1/carrito", "motor", "--quien", "brayan", "--motivo", "x"], docker)
    assert code == script.EXIT_USAGE and "APROBADORES" in out and docker.calls == []


def test_a_refusal_of_the_base_exits_blocked_and_the_way_back_names_the_scope_version() -> None:
    docker = FakeDocker(answer={"ok": False, "reason_code": "flow_route_generation_stale", "generation": 1})
    code, out = run(MOVE, docker)
    assert code == script.EXIT_BLOCKED and "flow_route_generation_stale" in out
    code, out = run(BACK, FakeDocker(route_row("motor", 1)))
    assert code == script.EXIT_OK and "en la version 7" in out
