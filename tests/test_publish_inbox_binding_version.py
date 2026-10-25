"""El cambio de inbox de una instancia (scripts/publish_inbox_binding_version.py), con un docker falso.

Lo que se prueba aca es lo que el script decide antes y despues de llamar a la
base: que inbox acepta (el de Chatwoot sale de la captura de 2026-10-25,
tests/fixtures/inbox_binding_version/chatwoot_inboxes_20261025.json), que el
en seco termina en rollback y --aplicar en commit despues del codigo, que
exige aceptar los eventos guardados con la version vieja, que las lecturas van
en una transaccion de solo lectura con los valores en el bloque COPY, y que
--verificar mira el binding y el /ready. Lo que hace la base con cada llamada
lo prueba, contra un PostgreSQL real con la cadena aplicada,
tests/sql/followup_engine/real_postgres_inbox_binding_version.py.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publish_inbox_binding_version_script",
                                              ROOT / "scripts/publish_inbox_binding_version.py")
assert SPEC and SPEC.loader
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

CAPTURE = json.loads((ROOT / "tests/fixtures/inbox_binding_version/chatwoot_inboxes_20261025.json")
                     .read_text(encoding="utf-8"))
ENV = {"CONTENEDOR_DB": "maribel_db", "CONTENEDOR_CHATWOOT_DB": "infra_chatwoot-db", "BASE_CHATWOOT_DB": "infra",
       "CONTENEDOR_BRIDGE": "maribel_appointment-bridge"}
ARGS = ["--tenant", "lancemos", "--inbox-nuevo", "13", "--quien", "dan", "--motivo", "llego el numero"]


def inbox_row(inbox_id: int, *, account: int = 3, whatsapp: bool = True) -> dict:
    """La fila que devuelve SQL_CHATWOOT_INBOX, con la forma de la captura."""
    captured = CAPTURE["inboxes"][0 if whatsapp else 1]
    channel = CAPTURE["channel_whatsapp"][0]
    return {"id": inbox_id, "account_id": account, "channel_type": captured["channel_type"],
            "enable_auto_assignment": captured["enable_auto_assignment"],
            "provider": channel["provider"] if whatsapp else None,
            "phone_number_present": channel["phone_number_present"] if whatsapp else False,
            "templates_approved": sum(t["status"] == "APPROVED" for t in channel["message_templates"]) if whatsapp else 0,
            "template_names": []}


def current(inbox: int = 12, previous: list[int] | None = None, controls: list[dict] | None = None) -> dict:
    return {"bindings": [{"funnel_ref": "maribel", "binding_version": 1, "chatwoot_account_id": 3,
                          "chatwoot_inbox_id": inbox, "inbound_scope_key": "maribel-inbound",
                          "inbound_scope_version": 1}],
            "previous_inboxes": previous or [],
            "controls": controls if controls is not None else [
                {"scope_key": "maribel-recuperacion", "scope_version": 1, "runtime_state": "inactive",
                 "chatwoot_inbox_id": inbox, "channel_account_ref": f"chatwoot-inbox:{inbox}"}]}


def plan(*, events: int = 0) -> dict:
    return {"tenant_ref": "lancemos", "from_inbox": 12, "to_inbox": 13, "from_ref": "chatwoot-inbox:12",
            "to_ref": "chatwoot-inbox:13",
            "bindings": [{"funnel_ref": "maribel", "from_version": 1, "to_version": 2, "from_inbox": 12,
                          "to_inbox": 13, "inbound_scope_key": "maribel-inbound"}],
            "purchase_policies": [{"funnel_ref": "maribel", "from_version": 1, "to_version": 2}],
            "inbound_scopes": [{"scope_key": "maribel-inbound", "from_version": 1, "to_version": 2}],
            "catalog_rows": 1,
            "handoff_policies": [{"policy_key": "maribel-derivacion-entrante", "from_version": 1, "to_version": 2,
                                  "scope_key": "maribel-inbound"}],
            "pilot_scopes": [{"scope_key": "maribel-recuperacion", "from_version": 1, "to_version": 2,
                              "from_state": "inactive", "runtime_state": "inactive",
                              "from_ref": "chatwoot-inbox:12", "to_ref": "chatwoot-inbox:13"}],
            "pilot_scopes_untouched": [],
            "old_version_rows": {"hotmart_event_bindings": events, "hotmart_purchase_correlations": 0,
                                 "post_inbound_discount_bindings": 0},
            "old_inbox_rows": {"channel_identities": 0, "commercial_cases": 0, "daily_feedback_schedules": 0}}


class FakeDocker:
    def __init__(self, *, inbox: dict | None = None, state: dict | None = None, result: dict | None = None,
                 ready: tuple[int, dict] = (200, {"status": "ready", "commercial_ally_binding": "active"}),
                 fail_with: str | None = None, containers: int = 1) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.inbox = inbox_row(13) if inbox is None else inbox
        self.state = current() if state is None else state
        self.result = plan() if result is None else result
        self.ready = ready
        self.fail_with = fail_with
        self.containers = containers

    def publishes(self) -> list[str]:
        return [stdin for argv, stdin in self.calls if "do $publicar$" in stdin]

    def run(self, argv, stdin=""):
        argv = list(argv)
        self.calls.append((argv, stdin))
        if argv[:1] == ["ps"]:
            names = "".join(f"{argv[2].removeprefix('name=')}.{i}\n" for i in range(self.containers))
            return subprocess.CompletedProcess(argv, 0, names, "")
        if argv[:2] == ["exec", "-i"] and argv[3:4] == ["python"]:
            code, ready = self.ready
            return subprocess.CompletedProcess(argv, 0, json.dumps({"code": code, "ready": ready}) + "\n", "")
        assert argv[:2] == ["exec", "-i"] and argv[3:5] == ["psql", "-X"], argv
        if "do $publicar$" in stdin:
            if self.fail_with:
                return subprocess.CompletedProcess(argv, 3, "", f"ERROR:  {self.fail_with}\n")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.result) + "\n", "")
        if "from public.inboxes" in stdin:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.inbox) + "\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.state) + "\n", "")


class FakeTerminal:
    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer if self.answer is not None else re.search(r"escribi ([0-9a-z]+):", prompt).group(1)


def run(docker: FakeDocker, *argv: str, terminal: FakeTerminal | None = None) -> tuple[int, str]:
    ops = script.Ops(script.Config.from_env(ENV, needs_bridge=True), docker=docker,
                     terminal=terminal or FakeTerminal(), out=io.StringIO(), code=lambda: "abc123")
    code = script.main(list(argv), ops=ops)
    return code, ops.out.getvalue()


def test_dry_run_rolls_back_and_says_what_to_change() -> None:
    docker = FakeDocker()
    code, out = run(docker, *ARGS)
    assert code == 0
    [publish] = docker.publishes()
    assert publish.rstrip().endswith("rollback;")
    assert "commit;" not in publish.split("$publicar$;", 1)[1]
    for line in ("binding maribel: v1 (retired) -> v2 (active)", "CHATWOOT_INBOX_ID=13",
                 "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF=chatwoot-inbox:13", "CHATWOOT_CUT_B_SCOPE_VERSION=2",
                 "HANDOFF_PROJECTION_POLICY_VERSION=2", "[instancia].binding_version = 2",
                 "[chatwoot].inbox_id = 13", "[inbound].scope_version = 2", "hace falta redeploy",
                 "--inbox-nuevo 12", "EN SECO"):
        assert line in out


def test_apply_asks_the_code_and_commits() -> None:
    docker, terminal = FakeDocker(), FakeTerminal()
    code, out = run(docker, *ARGS, "--aplicar", terminal=terminal)
    assert code == 0 and "APLICADO" in out
    dry, applied = docker.publishes()
    assert dry.rstrip().endswith("rollback;") and applied.rstrip().endswith("commit;")
    assert terminal.prompts == ["para publicar el inbox 13 en lancemos, escribi abc123: "]


def test_wrong_code_writes_nothing() -> None:
    docker = FakeDocker()
    code, out = run(docker, *ARGS, "--aplicar", terminal=FakeTerminal("zzz"))
    assert code == 1 and "el codigo no coincide" in out
    assert [p.rstrip()[-9:] for p in docker.publishes()] == ["rollback;"]


def test_apply_requires_accepting_rows_of_the_old_binding_version() -> None:
    docker = FakeDocker(result=plan(events=9))
    code, out = run(docker, *ARGS, "--aplicar")
    assert code == 1 and "--acepto-eventos-de-la-version-vieja" in out and "hotmart_event_bindings=9" in out
    assert len(docker.publishes()) == 1
    docker = FakeDocker(result=plan(events=9))
    code, out = run(docker, *ARGS, "--aplicar", "--acepto-eventos-de-la-version-vieja")
    assert code == 0 and docker.publishes()[-1].rstrip().endswith("commit;")


@pytest.mark.parametrize(("inbox", "state", "expected"), [
    (inbox_row(13, account=2), None, "es de la cuenta 2 de Chatwoot"),
    (inbox_row(13, whatsapp=False), None, "es Channel::Api, no de WhatsApp"),
    (None, None, "no existe en Chatwoot"),
    (inbox_row(13), current(inbox=13), "ya tiene el binding activo"),
    (inbox_row(13), {"bindings": [], "controls": []}, "ningun binding activo"),
])
def test_refuses_an_inbox_that_does_not_fit(inbox, state, expected) -> None:
    docker = FakeDocker(inbox=inbox if inbox is not None else {}, state=state)
    if inbox is None:
        docker.inbox = None  # SQL_CHATWOOT_INBOX devuelve null
    code, out = run(docker, *ARGS, "--aplicar")
    assert code == 1 and expected in out
    assert docker.publishes() == []


def test_going_back_to_the_previous_api_inbox_is_allowed() -> None:
    docker = FakeDocker(inbox=inbox_row(12, whatsapp=False), state=current(inbox=13, previous=[12]))
    code, out = run(docker, "--tenant", "lancemos", "--inbox-nuevo", "12", "--quien", "dan", "--motivo", "volver")
    assert code == 0 and "se vuelve a una bandeja que ya tuvo el binding" in out


def test_warns_about_auto_assignment_and_missing_templates() -> None:
    row = {**inbox_row(13), "enable_auto_assignment": True, "templates_approved": 0}
    code, out = run(FakeDocker(inbox=row), *ARGS)
    assert code == 0 and "asignacion automatica del inbox esta prendida" in out and "no tiene plantillas" in out


def test_database_refusal_is_explained() -> None:
    code, out = run(FakeDocker(fail_with="inbox_binding_control_armed"), *ARGS)
    assert code == 1 and "inbox_binding_control_armed" in out and "piloto.sh pausar" in out


def test_values_go_in_the_copy_block_and_reads_are_read_only() -> None:
    docker = FakeDocker()
    run(docker, *ARGS)
    for argv, stdin in docker.calls:
        if argv[3:5] != ["psql", "-X"]:
            continue
        body = stdin.split("\\.\n", 1)[1]
        for value in ("'lancemos'", "llego el numero", "'dan'"):
            assert value not in body
        if "do $publicar$" not in stdin:
            assert "begin transaction read only;" in body
    chatwoot = [argv for argv, stdin in docker.calls if "from public.inboxes" in stdin]
    assert chatwoot and all(argv[2] == "infra_chatwoot-db.0" and argv[-1] == "infra" for argv in chatwoot)


def test_container_filter_must_match_exactly_one() -> None:
    code, out = run(FakeDocker(containers=2), *ARGS)
    assert code == 1 and "exactamente un contenedor" in out


def test_usage_requires_who_and_why_to_publish() -> None:
    code, out = run(FakeDocker(), "--tenant", "lancemos", "--inbox-nuevo", "13")
    assert code == 2 and "--quien es obligatorio" in out
    code, out = run(FakeDocker(), "--tenant", "Lancemos!", "--inbox-nuevo", "13", "--quien", "dan", "--motivo", "x")
    assert code == 2


def test_verify_passes_with_the_new_inbox_and_ready_active() -> None:
    state = current(inbox=13)
    code, out = run(FakeDocker(state=state), "--tenant", "lancemos", "--inbox-nuevo", "13", "--verificar")
    assert code == 0 and out.rstrip().endswith("VERIFICADO") and "NO VERIFICADO" not in out


def test_verify_fails_before_the_redeploy() -> None:
    docker = FakeDocker(state=current(inbox=13),
                        ready=(503, {"detail": "commercial_ally_binding_unavailable"}))
    code, out = run(docker, "--tenant", "lancemos", "--inbox-nuevo", "13", "--verificar")
    assert code == 1 and "NO VERIFICADO" in out and "redeploy" in out


def test_verify_fails_when_the_binding_still_has_the_old_inbox() -> None:
    code, out = run(FakeDocker(), "--tenant", "lancemos", "--inbox-nuevo", "13", "--verificar")
    assert code == 1 and "tiene el inbox 12" in out


def test_verify_needs_the_bridge_container() -> None:
    env = {k: v for k, v in ENV.items() if k != "CONTENEDOR_BRIDGE"}
    out = io.StringIO()
    with pytest.raises(script.Refused, match="CONTENEDOR_BRIDGE"):
        script.Config.from_env(env, needs_bridge=True)
    assert script.Config.from_env(env, needs_bridge=False).bridge_filter == ""
    assert out.getvalue() == ""
