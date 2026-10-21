"""The operator command of the flow store, end to end, on a real PostgreSQL.

``scripts/flow_change_operator.py`` runs on the VPS against the database of the
instance with ``docker exec ... psql`` and runs the validator in a fresh
container of the bridge image. Here the same script runs against the
PostgreSQL of the CI with the whole migration chain applied: ``docker`` is a
shim that sends ``exec ... psql`` to the local ``psql`` and ``run ... -m
bridge.flow_change_operator`` to this checkout's module, and the terminal is a
shim that types the code the script shows. Nothing else is replaced: every
write goes through the seven ``operator_*`` functions of 20261016000100, with
their locks, their checks and their events.

The catalog is read from a second database with the two Chatwoot tables the
script reads (``inboxes`` and ``channel_whatsapp``) and the templates of inbox
11 captured from the Chatwoot of ATT1 on 2026-10-21
(tests/fixtures/flow_change_operator/).

What it proves, in order:

1. ``proponer`` with the v1 of the cart, the failed payment and the first
   contact (tests/fixtures/flow_store/att1_definitions_20261016.json) saves a
   draft and validates it; ``publicar --sin-simulacion`` approves and publishes
   the three, off;
2. ``prender att1/carrito`` turns it on in one command;
3. a v2 of the cart that waits 2 hours before the template: ``publicar``
   without ``--sin-simulacion`` refuses and writes nothing (the proposal stays
   ``validada``); with it, the cart goes to v2;
4. a wrong confirmation code writes nothing;
5. ``volver att1/carrito 1`` goes back to v1 without a new version;
6. ``pausar att1/carrito`` pauses it with the generation it read;
7. ``descartar`` closes an open proposal;
8. every psql call carried its values in the COPY block, never in the SQL text.

With the engine migration (20261021000200) applied, it also proves the T4d and
the criterion of the T4 (flow-change-cycle-v1, 1.4.0 and 1.5.0):

0. ``registrar att1/precheckout`` registers a new flow, off and without a
   version, and refuses one that is already registered;
5b. ``ruta`` refuses a flow that is off and creates the route of the cart in
   ``legado`` on a published scope of the legacy; ``estado`` shows it;
5c. a wait in the cart, proposed and validated by the command, carries a
   built simulation read from the base; ``publicar`` without
   ``--sin-simulacion`` publishes it and the approval records
   ``simulation: built`` with the ``summary_sha256`` of the preview.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_VERSION = "20261016000100"
FIXTURES = ROOT / "tests" / "fixtures"
STORE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_store", ROOT / "tests/sql/followup_engine/real_postgres_flow_store.py")
OPERATOR_SPEC = importlib.util.spec_from_file_location("flow_change_operator", ROOT / "scripts/flow_change_operator.py")
assert STORE_SPEC and STORE_SPEC.loader and OPERATOR_SPEC and OPERATOR_SPEC.loader
store = importlib.util.module_from_spec(STORE_SPEC)
STORE_SPEC.loader.exec_module(store)
operator = importlib.util.module_from_spec(OPERATOR_SPEC)
sys.modules[OPERATOR_SPEC.name] = operator
OPERATOR_SPEC.loader.exec_module(operator)
# El scope del legado publicado que pide la ruta: el mismo que siembra el arnes del motor.
ENGINE_SPEC = importlib.util.spec_from_file_location(
    "real_postgres_flow_engine", ROOT / "tests/sql/followup_engine/real_postgres_flow_engine.py")
assert ENGINE_SPEC and ENGINE_SPEC.loader
ENGINE = importlib.util.module_from_spec(ENGINE_SPEC)
ENGINE_SPEC.loader.exec_module(ENGINE)

PURPOSE = "que el carrito espere 2 horas antes del primer mensaje"
DEFINITIONS = {d["flujo"]: d for d in json.loads(
    (FIXTURES / "flow_store/att1_definitions_20261016.json").read_text(encoding="utf-8"))["definitions"]}
TEMPLATES = json.loads((FIXTURES / "flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json")
                       .read_text(encoding="utf-8"))["message_templates"]
ENV = {
    "CONTENEDOR_DB": "att1-db", "CONTENEDOR_CHATWOOT_DB": "chatwoot-db", "BASE_CHATWOOT_DB": "chatwoot",
    "BANDEJA_CHATWOOT": "11", "IMAGEN_BRIDGE": "setter-bridge@sha256:" + "d" * 64,
    "TENANT": "lancemos", "ALIADA": "att1", "ZONA": "America/Mexico_City", "APROBADORES": "dan",
}


def chatwoot_env() -> dict[str, str]:
    env = store.pg_env()
    env["PGDATABASE"] = env["PGDATABASE"] + "_chatwoot"
    return env


class ShimDocker:
    """``docker``, sin docker: psql local y el modulo de este checkout."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.writes = 0

    def run(self, argv, stdin=""):
        argv = list(argv)
        if argv[:1] == ["ps"]:
            name = argv[2].removeprefix("name=")
            return subprocess.CompletedProcess(argv, 0, name + "\n", "")
        if argv[:2] == ["exec", "-i"]:
            container, psql_args = argv[2], argv[3:]
            assert psql_args[:2] == ["psql", "-X"], psql_args
            cleaned = [store.PSQL]
            rest = iter(psql_args[1:])
            for arg in rest:
                if arg in ("-U", "-d"):
                    next(rest)  # el usuario y la base los pone el entorno local
                    continue
                cleaned.append(arg)
            self.scripts.append(stdin)
            if "select public.operator_" in stdin:
                self.writes += 1
            env = store.pg_env() if container == "att1-db" else chatwoot_env()
            return subprocess.run(cleaned, input=stdin, text=True, capture_output=True, env=env, check=False)
        if argv[:1] == ["run"]:
            assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
            assert "--read-only" in argv
            subcommand = argv[-1]
            env = {**os.environ, "SETTER_VERSION": "1.15.5", "GIT_SHA": "b163669",
                   "PYTHONPATH": str(ROOT / "src")}
            return subprocess.run([sys.executable, "-m", "bridge.flow_change_operator", subcommand],
                                  input=stdin, text=True, capture_output=True, env=env, check=False)
        raise AssertionError(f"unexpected docker call: {argv[:2]}")


class ShimTerminal:
    def __init__(self) -> None:
        self.wrong_next = False

    def ask(self, prompt: str) -> str:
        code = re.search(r"codigo: ([0-9a-f]+)", prompt)
        assert code, prompt
        if self.wrong_next:
            self.wrong_next = False
            return "000000"
        return code.group(1)


def install() -> None:
    # La guarda del arnes del almacen: ALLOW_DISPOSABLE_DATABASE=flow-store y una
    # base vacia que empieza con flow_store (flow_store_operator_ci en la CI).
    store.install_schema()
    store.seed()  # el binding de att1 y el carrito y el pago fallido registrados
    chatwoot = chatwoot_env()["PGDATABASE"]
    store.query(f'drop database if exists "{chatwoot}"')
    store.query(f'create database "{chatwoot}"')
    templates = json.dumps(TEMPLATES, ensure_ascii=True).replace("'", "''")
    result = subprocess.run(
        [store.PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1"],
        input=("create table inboxes (id integer primary key, channel_id integer, channel_type text);\n"
               "create table channel_whatsapp (id integer primary key, message_templates jsonb);\n"
               f"insert into channel_whatsapp values (7, '{templates}'::jsonb);\n"
               "insert into inboxes values (11, 7, 'Channel::Whatsapp');\n"),
        text=True, capture_output=True, env=chatwoot_env(), check=False)
    store.require(result.returncode == 0, result.stderr)


def run(ops, *argv: str) -> tuple[int, str]:
    ops.out = io.StringIO()
    code = operator.main(list(argv), ops=ops)
    return code, ops.out.getvalue()


def controls() -> dict[str, tuple[str, str, str]]:
    lines = store.query("select flow_key, state, coalesce(active_version::text, '-'), generation::text "
                        "from public.flow_controls order by flow_key").splitlines()
    return {line.split("|")[0]: tuple(line.split("|")[1:]) for line in lines}


def statuses() -> list[str]:
    return store.query("select status from public.flow_change_proposals order by created_at").splitlines()


def items_file(items) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(items, handle)
    handle.close()
    return handle.name


def flow_ids() -> dict[str, str]:
    lines = store.query("select flow_key, flow_id from public.flow_controls").splitlines()
    return dict(line.split("|") for line in lines)


def main() -> None:
    install()
    docker, terminal = ShimDocker(), ShimTerminal()
    ops = operator.Ops(operator.Config.from_env(ENV), docker=docker, terminal=terminal)
    # 0. Registrar el precheckout con el comando: apagado y sin version; el que ya esta, no.
    code, out = run(ops, "registrar", "att1/precheckout", "--quien", "dan", "--motivo", "alta del flujo")
    store.require(code == 0 and "apagado y sin version" in out, out)
    writes = docker.writes
    code, out = run(ops, "registrar", "att1/carrito", "--quien", "dan", "--motivo", "otra vez")
    store.require(code == 1 and "ya esta registrado" in out and docker.writes == writes, out)
    ids = flow_ids()

    # 1. Las tres v1, propuestas, validadas y publicadas apagadas.
    v1 = [{"producto": "att1", "flujo": key, "flow_id": ids[key], "tipo": "version",
           "definicion": DEFINITIONS[key], "en_curso": "seguir"} for key in ("carrito", "pago_fallido", "precheckout")]
    code, out = run(ops, "proponer", "--archivo", items_file(v1), "--quien", "dan", "--motivo", "la v1 de ATT1")
    store.require(code == 0, out)
    proposal_id = re.search(r"publicar ([0-9a-f-]{36})", out).group(1)
    code, out = run(ops, "publicar", proposal_id, "--quien", "dan", "--sin-simulacion")
    store.require(code == 0, out)
    store.require(controls() == {"carrito": ("apagado", "1", "1"), "pago_fallido": ("apagado", "1", "1"),
                                 "precheckout": ("apagado", "1", "1")}, f"after v1: {controls()}")
    print("flow_change_operator_v1=OK")

    # 2. Prender el carrito.
    code, out = run(ops, "prender", "att1/carrito", "--quien", "dan", "--motivo", "prender el carrito",
                    "--sin-simulacion")
    store.require(code == 0, out)
    store.require(controls()["carrito"] == ("prendido", "1", "2"), f"after prender: {controls()}")

    # 3. La v2 con la espera: sin --sin-simulacion no publica ni escribe.
    v2 = copy.deepcopy(DEFINITIONS["carrito"])
    v2["pasos"] = [{"esperar": "2h"}, *v2["pasos"]]
    item = [{"producto": "att1", "flujo": "carrito", "flow_id": ids["carrito"], "tipo": "version",
             "definicion": v2, "en_curso": "seguir"}]
    code, out = run(ops, "proponer", "--archivo", items_file(item), "--quien", "dan", "--motivo", PURPOSE)
    store.require(code == 0, out)
    v2_id = re.search(r"publicar ([0-9a-f-]{36})", out).group(1)
    writes = docker.writes
    code, out = run(ops, "publicar", v2_id, "--quien", "dan")
    store.require(code == 1 and "--sin-simulacion" in out, out)
    store.require(docker.writes == writes and statuses()[-1] == "validada", "a refused publish wrote")
    store.require('"/pasos/0/esperar": null -> "2h"' in out, f"the diff of the script is missing: {out}")

    # 4. Un codigo equivocado no escribe.
    terminal.wrong_next = True
    code, out = run(ops, "publicar", v2_id, "--quien", "dan", "--sin-simulacion")
    store.require(code == 1 and docker.writes == writes and statuses()[-1] == "validada", out)
    code, out = run(ops, "publicar", v2_id, "--quien", "dan", "--sin-simulacion")
    store.require(code == 0, out)
    store.require(controls()["carrito"] == ("prendido", "2", "3"), f"after v2: {controls()}")
    print("flow_change_operator_v2=OK")

    # 5. Volver a la v1, sin version nueva.
    code, out = run(ops, "volver", "att1/carrito", "1", "--quien", "dan", "--motivo", "volver a la v1",
                    "--sin-simulacion")
    store.require(code == 0, out)
    store.require(controls()["carrito"] == ("prendido", "1", "4"), f"after volver: {controls()}")
    store.require(store.query("select count(*) from public.flow_definitions") == "4", "volver wrote a version")

    # 5b. La ruta del carrito en legado: no sobre un flujo apagado, si sobre el carrito prendido.
    ENGINE.seed_scope()
    writes = docker.writes
    code, out = run(ops, "ruta", "att1/pago_fallido", "--tipo", "pago_fallido", "--scope", "att1-recuperacion",
                    "--quien", "dan", "--motivo", "x")
    store.require(code == 1 and "se frenaria al instante" in out and docker.writes == writes, out)
    code, out = run(ops, "ruta", "att1/carrito", "--tipo", "carrito", "--scope", "att1-recuperacion",
                    "--quien", "dan", "--motivo", "el carrito, frenable desde el almacen")
    store.require(code == 0 and "frena al legado" in out, out)
    route = store.query("select r.executor, r.legacy_kind, r.legacy_scope_key from public.flow_engine_routes r "
                        f"where r.flow_id = '{ids['carrito']}'")
    store.require(route == "legado|carrito|att1-recuperacion", f"the route: {route}")
    code, out = run(ops, "estado")
    store.require("ruta: legado (carrito, att1-recuperacion); pausarlo o apagarlo aca frena el legado" in out, out)
    print("flow_change_operator_route=OK")

    # 5c. El criterio de la T4: una espera con su simulacion, publicada sin --sin-simulacion, y aprobar
    # registra simulation: built con el summary_sha256 de la vista previa.
    code, out = run(ops, "proponer", "--archivo", items_file(item), "--quien", "dan", "--motivo", PURPOSE)
    store.require(code == 0, out)
    built_id = re.search(r"publicar ([0-9a-f-]{36})", out).group(1)
    stored = json.loads(store.query("select validation #> '{preview,simulacion}' from public.flow_change_proposals "
                                    f"where id = '{built_id}'"))
    store.require(stored.get("status") == "built", f"the stored simulation: {stored}")
    code, out = run(ops, "publicar", built_id, "--quien", "dan")
    store.require(code == 0, out)
    approved = store.query("select data->>'simulation', data->>'summary_sha256' from public.flow_change_events "
                           f"where proposal_id = '{built_id}' and event_type = 'flow_change_approved'")
    store.require(approved == f"built|{stored['summary_sha256']}", f"the approval: {approved}")
    store.require(controls()["carrito"][:2] == ("prendido", "3"), f"after the built publish: {controls()}")
    print("flow_change_operator_simulation_built=OK")

    # 6. Pausar con la generacion leida.
    code, out = run(ops, "pausar", "att1/carrito", "--quien", "dan", "--motivo", "pausa de prueba")
    store.require(code == 0, out)
    store.require(controls()["carrito"] == ("pausado", "3", "6"), f"after pausar: {controls()}")

    # 7. Descartar una propuesta abierta.
    code, out = run(ops, "proponer", "--archivo", items_file(item), "--quien", "dan", "--motivo", PURPOSE)
    store.require(code == 0, out)
    open_id = re.search(r"publicar ([0-9a-f-]{36})", out).group(1)
    code, out = run(ops, "descartar", open_id, "--quien", "dan", "--motivo", "ya no va")
    store.require(code == 0 and statuses()[-1] == "descartada", out)

    # Quien no esta en APROBADORES no escribe.
    writes = docker.writes
    code, out = run(ops, "pausar", "att1/pago_fallido", "--quien", "brayan", "--motivo", "x")
    store.require(code == 2 and docker.writes == writes, out)

    # 8. Los valores nunca en el texto del SQL.
    for script in docker.scripts:
        sql = script.split("\n\\.\n", 1)[1]
        for value in (PURPOSE, "ya no va", "pausa de prueba", "att1_carrito_abandonado_01"):
            store.require(value not in sql, f"a value went into the SQL text: {value}")
    events = store.query("select event_type, count(*) from public.flow_change_events group by 1 order by 1")
    print("flow_change_operator_events=" + events.replace("\n", ","))
    print("flow_change_operator_real_postgres=OK")


if __name__ == "__main__":
    main()
