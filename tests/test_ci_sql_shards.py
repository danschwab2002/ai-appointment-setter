"""La CI partida (claude-ci-split-v1): que partirla no deje nada sin correr.

verify sigue siendo el check requerido de main, pero ahora junta tres cosas:
el job de Python y Postgres real, y la suite de PGlite en tandas. Lo que estas
pruebas cuidan es lo que se pierde en silencio al partir una CI:

- una tanda que nadie corre (la matriz y --of no coinciden);
- un job que falla y verify igual pasa (verify no mira a todos);
- un script que no cae en ninguna tanda, o cae en dos;
- un paso de Postgres real que se perdio al mover los pasos de job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SQL_DIR = ROOT / "tests" / "sql" / "followup_engine"
RUNNER = SQL_DIR / "run_shard.mjs"
WEIGHTS = SQL_DIR / "shard_weights.json"

# Lo que corria el job verify de antes del split, fuera de la suite de PGlite.
POSTGRES_PROBES = (
    "tests/sql/followup_engine/validate_handoff_postgres.py",
    "tests/sql/followup_engine/real_postgres_observed_lead.py",
    "tests/sql/followup_engine/real_postgres_hotmart_intent_correlation.py",
    "tests/sql/followup_engine/real_postgres_portable_precheckout_first_contact.py",
    "tests/sql/followup_engine/real_postgres_portable_inbound_adoption.py",
    "tests/sql/followup_engine/real_postgres_flow_store.py",
)
SHARD_RUN = re.compile(r"node run_shard\.mjs --shard \$\{\{ matrix\.shard \}\} --of (\d+)")


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _shard_job() -> tuple[int, list[str]]:
    job = _jobs()["sql-pglite"]
    text = " ".join(_run_text(job).split())
    match = SHARD_RUN.search(text)
    assert match, "sql-pglite no corre run_shard.mjs con --shard de la matriz"
    also = re.findall(r"--also (\S+)", text)
    return int(match.group(1)), also


def _test_commands() -> list[str]:
    package = json.loads((SQL_DIR / "package.json").read_text(encoding="utf-8"))
    return [part.strip() for part in package["scripts"]["test"].split(" && ")]


def _node() -> str:
    node = shutil.which("node")
    assert node, "las pruebas del reparto necesitan node (en la CI viene en ubuntu-latest)"
    return node


def _runner(*args: str, cwd: Path = SQL_DIR, runner: Path = RUNNER) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), str(runner), *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False
    )


def test_verify_waits_for_every_job_and_requires_each_to_succeed() -> None:
    jobs = _jobs()
    verify = jobs["verify"]
    others = sorted(name for name in jobs if name != "verify")
    assert sorted(verify["needs"]) == others
    # Sin always(), un job que falla deja a verify en skipped, y skipped no es un rojo claro.
    assert verify["if"] == "${{ always() }}"
    step = verify["steps"][0]
    env = step["env"]
    run = step["run"]
    # Las comparaciones van encadenadas con &&: con otro shell (bash sin -e) o
    # con un || true, una que falla dejaria de cortar y decidiria la ultima.
    assert "shell" not in step and "shell" not in verify
    assert "defaults" not in yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert "||" not in run and ";" not in run
    lines = [line for line in run.splitlines() if line.strip().startswith("test ")]
    assert len(lines) == 1, run
    checks = lines[0].strip().split(" && ")
    assert len(checks) == len(others)
    for name in others:
        variables = [key for key, value in env.items() if value == f"${{{{ needs.{name}.result }}}}"]
        assert len(variables) == 1, f"verify no lee el resultado de {name}"
        assert f'test "${variables[0]}" = success' in checks


def test_no_job_or_step_can_be_skipped_or_fail_without_failing_the_run() -> None:
    for name, job in _jobs().items():
        assert "continue-on-error" not in job, name
        # Un paso con if: queda skipped y su job da success: verify no lo ve.
        if name != "verify":
            assert "if" not in job, name
        for step in job["steps"]:
            assert "continue-on-error" not in step, f"{name}: {step.get('name')}"
            assert "if" not in step, f"{name}: {step.get('name')}"


def test_the_matrix_runs_every_shard_that_the_runner_splits_into() -> None:
    of, _ = _shard_job()
    strategy = _jobs()["sql-pglite"]["strategy"]
    assert strategy["matrix"]["shard"] == list(range(1, of + 1))
    assert _jobs()["sql-pglite"]["name"] == f"sql-pglite (${{{{ matrix.shard }}}}/{of})"
    # Con fail-fast, la primera tanda roja cancela las otras y su rojo no se ve.
    assert strategy["fail-fast"] is False


def test_the_python_job_keeps_every_step_that_verify_ran_before_the_split() -> None:
    job = _jobs()["python-and-postgres"]
    text = _run_text(job)
    assert "scripts/agent_workspace.py validate-tree" in text
    assert "uv run pytest -q" in text
    for probe in POSTGRES_PROBES:
        assert probe in text, probe
    assert "postgres" in job["services"]


def test_the_extra_scripts_exist_and_are_not_already_in_npm_test() -> None:
    _, also = _shard_job()
    assert also == ["validate_pilot_boundary.mjs", "validate_pilot_boundary_runtime.mjs"]
    commands = _test_commands()
    for file in also:
        assert (SQL_DIR / file).is_file(), file
        assert f"node {file}" not in commands


def test_the_weights_only_name_scripts_that_run() -> None:
    _, also = _shard_job()
    commands = set(_test_commands()) | {f"node {file}" for file in also}
    seconds = json.loads(WEIGHTS.read_text(encoding="utf-8"))["seconds"]
    assert set(seconds) <= commands, sorted(set(seconds) - commands)
    assert all(isinstance(value, (int, float)) and value > 0 for value in seconds.values())


def test_the_plan_is_a_partition_of_npm_test_and_the_extras() -> None:
    of, also = _shard_job()
    extra = [arg for file in also for arg in ("--also", file)]
    result = _runner("--plan", "--of", str(of), *extra)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    planned = [command for shard in plan["shards"] for command in shard["commands"]]
    expected = _test_commands() + [f"node {file}" for file in also]
    assert sorted(planned) == sorted(expected)
    assert len(planned) == len(set(planned))
    assert [shard["shard"] for shard in plan["shards"]] == list(range(1, of + 1))
    assert all(shard["commands"] for shard in plan["shards"])
    # El orden de package.json se respeta adentro de cada tanda.
    for shard in plan["shards"]:
        positions = [expected.index(command) for command in shard["commands"]]
        assert positions == sorted(positions)


def test_the_runner_rejects_a_bad_split() -> None:
    cases = (
        ("--shard", "4", "--of", "3"),
        ("--plan", "--of", "999"),
        ("--plan", "--of", "3", "--also", "no_existe.mjs"),
        ("--plan", "--of", "3", "--also", "validate.mjs"),
        ("--plan", "--of", "0"),
        ("--plan",),
        ("--shard", "1", "--of", "3", "--otra"),
    )
    for args in cases:
        result = _runner(*args)
        assert result.returncode == 2, (args, result.stdout, result.stderr)
        assert "run_shard:" in result.stderr, args


def test_a_failing_script_stops_its_shard_with_its_code(tmp_path: Path) -> None:
    shutil.copy(RUNNER, tmp_path / "run_shard.mjs")
    marker = tmp_path / "ran.txt"
    (tmp_path / "ok.mjs").write_text(
        f"import {{ appendFileSync }} from 'node:fs'; appendFileSync({json.dumps(str(marker))}, 'ok\\n');\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.mjs").write_text("process.exit(7);\n", encoding="utf-8")
    (tmp_path / "never.mjs").write_text(
        f"import {{ appendFileSync }} from 'node:fs'; appendFileSync({json.dumps(str(marker))}, 'never\\n');\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"type": "module", "scripts": {"test": "node ok.mjs && node bad.mjs && node never.mjs"}}),
        encoding="utf-8",
    )
    (tmp_path / "shard_weights.json").write_text(json.dumps({"seconds": {}}), encoding="utf-8")

    result = _runner("--shard", "1", "--of", "1", cwd=tmp_path, runner=tmp_path / "run_shard.mjs")

    assert result.returncode == 7, result.stderr
    assert "fallo node bad.mjs (codigo 7" in result.stderr
    assert marker.read_text(encoding="utf-8") == "ok\n"


def _fake_suite(tmp_path: Path, scripts: dict[str, str], test: str, **extra: str) -> Path:
    shutil.copy(RUNNER, tmp_path / "run_shard.mjs")
    for file, body in scripts.items():
        (tmp_path / file).write_text(body, encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"type": "module", "scripts": {"test": test, **extra}}), encoding="utf-8"
    )
    (tmp_path / "shard_weights.json").write_text(json.dumps({"seconds": {}}), encoding="utf-8")
    return tmp_path / "run_shard.mjs"


def test_a_script_killed_by_a_signal_fails_its_shard(tmp_path: Path) -> None:
    runner = _fake_suite(tmp_path, {"killed.mjs": "process.kill(process.pid, 'SIGKILL');\n"}, "node killed.mjs")

    result = _runner("--shard", "1", "--of", "1", cwd=tmp_path, runner=runner)

    assert result.returncode == 1, result.stderr
    assert "senal SIGKILL" in result.stderr


def test_the_runner_refuses_npm_hooks_that_the_shards_would_skip(tmp_path: Path) -> None:
    for hook in ("pretest", "posttest"):
        runner = _fake_suite(tmp_path, {"ok.mjs": ""}, "node ok.mjs", **{hook: "node ok.mjs"})

        result = _runner("--plan", "--of", "1", cwd=tmp_path, runner=runner)

        assert result.returncode == 2, (hook, result.stderr)
        assert f"scripts.{hook}" in result.stderr
