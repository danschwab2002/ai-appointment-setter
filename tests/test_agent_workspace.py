"""Behavioral tests for the shared multi-agent worktree coordinator."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "agent_workspace.py"
SPEC = importlib.util.spec_from_file_location("agent_workspace", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
agent_workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_workspace)
AgentWorkspace = agent_workspace.AgentWorkspace
CoordinationError = agent_workspace.CoordinationError


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
        input=input,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Agent")
    _git(root, "config", "user.email", "agent@example.invalid")
    (root / "README.md").write_text("fixture\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n")
    (root / "supabase" / "migrations").mkdir(parents=True)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    return root


def _start(
    coordinator: AgentWorkspace,
    root: Path,
    task_id: str,
    *,
    paths: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
):
    if not paths and not resources:
        resources = (f"task:{task_id}",)
    return coordinator.start(
        task_id=task_id,
        title=f"Task {task_id}",
        owner=f"session-{task_id}",
        branch=f"feat/{task_id}",
        worktree=root.parent / f"worktree-{task_id}",
        base_ref="origin/main",
        paths=paths,
        resources=resources,
    )


def test_creates_isolated_claim_and_requires_it_for_preflight(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)

    claim = _start(
        coordinator,
        repo,
        "feature-a",
        paths=("src/app.py",),
        resources=("contract:feature-a",),
    )

    assert Path(claim.worktree).is_dir()
    assert _git(Path(claim.worktree), "branch", "--show-current") == "feat/feature-a"
    assert coordinator.preflight(Path(claim.worktree)).task_id == "feature-a"
    with pytest.raises(CoordinationError, match="protected branch"):
        coordinator.preflight(repo)


def test_failed_claim_write_preserves_concurrent_work(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = AgentWorkspace(repo)
    worktree = repo.parent / "worktree-feature-a"

    def fail_after_concurrent_write(claim, *, create=False):
        del create
        (Path(claim.worktree) / "rescue.txt").write_text("preserve me\n")
        raise OSError("simulated registry failure")

    monkeypatch.setattr(coordinator, "_write_claim", fail_after_concurrent_write)

    with pytest.raises(CoordinationError, match="preserved for recovery"):
        _start(coordinator, repo, "feature-a")

    assert (worktree / "rescue.txt").read_text() == "preserve me\n"
    assert _git(worktree, "branch", "--show-current") == "feat/feature-a"
    assert _git(repo, "show-ref", "--verify", "refs/heads/feat/feature-a")


def test_dirty_main_blocks_new_worktree(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    (repo / "README.md").write_text("dirty\n")

    with pytest.raises(CoordinationError, match="main worktree is dirty"):
        _start(coordinator, repo, "feature-a")


@pytest.mark.parametrize("path", ["/etc/passwd", r"C:\Windows\System32"])
def test_absolute_scope_path_is_rejected(repo: Path, path: str) -> None:
    coordinator = AgentWorkspace(repo)

    with pytest.raises(CoordinationError, match="repository-relative"):
        _start(coordinator, repo, "feature-a", paths=(path,))


def test_non_protected_base_ref_is_rejected(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)

    with pytest.raises(CoordinationError, match="remote protected branch"):
        coordinator.start(
            task_id="feature-a",
            title="Feature A",
            owner="session-a",
            branch="feat/feature-a",
            worktree=repo.parent / "feature-a",
            base_ref="origin/topic-base",
            resources=("task:feature-a",),
        )


def test_empty_claim_scope_is_rejected(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)

    with pytest.raises(CoordinationError, match="at least one path or resource"):
        coordinator.start(
            task_id="feature-a",
            title="Feature A",
            owner="session-a",
            branch="feat/feature-a",
            worktree=repo.parent / "feature-a",
            base_ref="origin/main",
        )


def test_overlapping_path_or_resource_is_rejected(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(
        coordinator,
        repo,
        "feature-a",
        paths=("src",),
        resources=("contract:purchase",),
    )

    with pytest.raises(CoordinationError, match="path overlap"):
        _start(coordinator, repo, "feature-b", paths=("src/app.py",))
    with pytest.raises(CoordinationError, match="resource overlap"):
        _start(
            coordinator,
            repo,
            "feature-c",
            paths=("docs/feature-c.md",),
            resources=("contract:purchase",),
        )


def test_unclaimed_worktree_fails_closed(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    unmanaged = repo.parent / "unmanaged"
    _git(repo, "worktree", "add", "-b", "feat/unmanaged", str(unmanaged), "main")

    with pytest.raises(CoordinationError, match="no active claim"):
        coordinator.preflight(unmanaged)


def test_duplicate_migration_version_across_active_claims_is_rejected(
    repo: Path,
) -> None:
    coordinator = AgentWorkspace(repo)
    claim_a = _start(
        coordinator,
        repo,
        "feature-a",
        paths=("src/a.py",),
        resources=("contract:a",),
    )
    claim_b = _start(
        coordinator,
        repo,
        "feature-b",
        paths=("src/b.py",),
        resources=("contract:b",),
    )
    migrations_a = Path(claim_a.worktree) / "supabase" / "migrations"
    migrations_b = Path(claim_b.worktree) / "supabase" / "migrations"
    migrations_a.mkdir(parents=True)
    migrations_b.mkdir(parents=True)
    (migrations_a / "20260809000100_feature_a.sql").write_text("select 1;\n")
    (migrations_b / "20260809000100_feature_b.sql").write_text("select 2;\n")

    with pytest.raises(CoordinationError, match="migration version overlap"):
        coordinator.preflight(Path(claim_a.worktree))


def test_review_requires_clean_published_branch(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a", paths=("README.md",))
    worktree = Path(claim.worktree)
    coordinator.transition("feature-a", "implementing")
    (worktree / "README.md").write_text("dirty\n")

    with pytest.raises(CoordinationError, match="worktree is dirty"):
        coordinator.transition("feature-a", "review")

    _git(worktree, "restore", "README.md")
    with pytest.raises(CoordinationError, match="branch is not published"):
        coordinator.transition("feature-a", "review")


def test_hook_blocks_protected_branch_and_allows_claimed_branch(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    coordinator.transition("feature-a", "implementing")

    with pytest.raises(CoordinationError, match="protected branch"):
        coordinator.hook_check(repo)
    assert coordinator.hook_check(Path(claim.worktree)).task_id == "feature-a"


def test_status_reports_unmanaged_and_claimed_worktrees(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a")
    unmanaged = repo.parent / "unmanaged"
    _git(repo, "worktree", "add", "-b", "feat/unmanaged", str(unmanaged), "main")

    status = coordinator.status()

    assert {item["management"] for item in status} == {
        "protected",
        "claimed",
        "unmanaged",
    }


def test_actual_file_overlap_is_detected_even_when_claims_declared_disjoint(
    repo: Path,
) -> None:
    coordinator = AgentWorkspace(repo)
    claim_a = _start(coordinator, repo, "feature-a", paths=("src/a.py",))
    claim_b = _start(coordinator, repo, "feature-b", paths=("src/b.py",))
    (Path(claim_a.worktree) / "README.md").write_text("agent a\n")
    (Path(claim_b.worktree) / "README.md").write_text("agent b\n")

    with pytest.raises(CoordinationError, match="changed file overlap"):
        coordinator.preflight(Path(claim_a.worktree))


def test_tracked_change_keeps_its_full_path_during_scope_check(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a", paths=("README.md",))
    (Path(claim.worktree) / "README.md").write_text("tracked change\n")

    assert coordinator.preflight(Path(claim.worktree)).task_id == "feature-a"


def test_terminal_claim_cannot_be_reopened(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a")
    coordinator.transition("feature-a", "abandoned")

    with pytest.raises(CoordinationError, match="terminal claim"):
        coordinator.transition("feature-a", "implementing")


def test_state_machine_rejects_skipping_review(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a")

    with pytest.raises(CoordinationError, match="invalid state transition"):
        coordinator.transition("feature-a", "merged")


def test_review_transition_runs_preflight_on_committed_changes(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a", paths=("src/app.py",))
    worktree = Path(claim.worktree)
    coordinator.transition("feature-a", "implementing")
    (worktree / "README.md").write_text("outside scope\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "outside scope")

    with pytest.raises(CoordinationError, match="outside declared scope"):
        coordinator.transition("feature-a", "review")


def test_overlapping_committed_changes_cannot_reach_review(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim_a = _start(coordinator, repo, "feature-a")
    claim_b = _start(coordinator, repo, "feature-b")
    coordinator.transition("feature-a", "implementing")
    coordinator.transition("feature-b", "implementing")
    for claim, value in ((claim_a, "agent a\n"), (claim_b, "agent b\n")):
        worktree = Path(claim.worktree)
        (worktree / "README.md").write_text(value)
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-m", f"change from {claim.task_id}")

    with pytest.raises(CoordinationError, match="changed file overlap"):
        coordinator.transition("feature-a", "review")


def test_dirty_terminal_worktree_blocks_new_claims(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    coordinator.transition("feature-a", "abandoned")
    (Path(claim.worktree) / "README.md").write_text("unexpected residue\n")

    with pytest.raises(CoordinationError, match="terminal claim has a dirty worktree"):
        _start(coordinator, repo, "feature-b")


def test_dirty_worktree_cannot_be_marked_abandoned(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    (Path(claim.worktree) / "README.md").write_text("valuable work\n")

    with pytest.raises(CoordinationError, match="use paused"):
        coordinator.transition("feature-a", "abandoned")


def test_paused_claim_keeps_its_scope_reserved(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a", paths=("src",))
    coordinator.transition("feature-a", "paused")

    with pytest.raises(CoordinationError, match="path overlap"):
        _start(coordinator, repo, "feature-b", paths=("src/app.py",))


def test_missing_reserving_worktree_blocks_new_claims(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    _git(repo, "worktree", "remove", str(claim.worktree))

    with pytest.raises(CoordinationError, match="worktree is missing"):
        _start(coordinator, repo, "feature-b")


def test_foreign_clone_cannot_counterfeit_reserving_worktree(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    worktree = Path(claim.worktree)
    _git(repo, "worktree", "remove", str(worktree))
    worktree.mkdir()
    _git(worktree, "init", "-b", "feat/feature-a")
    _git(worktree, "config", "user.email", "test@example.com")
    _git(worktree, "config", "user.name", "Test User")
    (worktree / "README.md").write_text("counterfeit\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "counterfeit")

    with pytest.raises(CoordinationError, match="identity mismatch"):
        _start(coordinator, repo, "feature-b")


def test_claim_scope_can_be_extended_only_when_disjoint(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a", paths=("src/a.py",))
    _start(coordinator, repo, "feature-b", paths=("src/b.py",))

    extended = coordinator.extend(
        "feature-a",
        paths=("docs/a.md",),
        resources=("contract:a",),
    )
    assert extended.paths == ["docs/a.md", "src/a.py"]
    assert extended.resources == ["contract:a"]

    with pytest.raises(CoordinationError, match="path overlap"):
        coordinator.extend("feature-a", paths=("src/b.py",))


def test_push_guard_rejects_main_ref(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)

    with pytest.raises(CoordinationError, match="direct push"):
        coordinator.check_push_input(
            ["refs/heads/feature-a abc refs/heads/main def"]
        )


def test_adopt_rejects_worktree_from_another_clone(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-b", "feat/other")

    coordinator = AgentWorkspace(repo)
    with pytest.raises(CoordinationError, match="different Git clone"):
        coordinator.adopt(
            task_id="foreign-worktree",
            title="Foreign",
            owner="session-foreign",
            worktree=other,
        )


def test_adopt_cannot_bypass_review_or_required_identity(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)

    with pytest.raises(CoordinationError, match="adopt state"):
        coordinator.adopt(
            task_id="feature-a",
            title="Feature A",
            owner="session-a",
            worktree=repo,
            state="review",
        )
    with pytest.raises(CoordinationError, match="title and owner"):
        coordinator.adopt(
            task_id="feature-b",
            title="",
            owner="",
            worktree=repo,
            state="paused",
        )


def test_install_hooks_selects_versioned_hook_directory(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    hooks = repo / ".githooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-push"):
        path = hooks / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    coordinator.install_hooks()

    assert _git(repo, "config", "--get", "core.hooksPath") == ".githooks"


def test_cleanup_rejects_nonterminal_claim(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    _start(coordinator, repo, "feature-a")

    with pytest.raises(CoordinationError, match="terminal claim"):
        coordinator.cleanup("feature-a")


def test_cleanup_rejects_worktree_reused_by_another_branch(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    worktree = Path(claim.worktree)
    coordinator.transition("feature-a", "abandoned")
    _git(worktree, "switch", "-c", "feat/unrelated")

    with pytest.raises(CoordinationError, match="identity no longer matches"):
        coordinator.cleanup("feature-a")

    assert worktree.is_dir()


def test_validate_tree_rejects_duplicate_migration_versions(repo: Path) -> None:
    migrations = repo / "supabase" / "migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    (migrations / "20260809000100_a.sql").write_text("select 1;\n")
    (migrations / "20260809000100_b.sql").write_text("select 2;\n")

    with pytest.raises(CoordinationError, match="duplicate migration versions"):
        AgentWorkspace(repo).validate_tree()


def test_rename_tracks_both_source_and_destination(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim_a = _start(coordinator, repo, "feature-a", paths=("README.md",))
    claim_b = _start(coordinator, repo, "feature-b", paths=("dest",))
    worktree_a = Path(claim_a.worktree)
    worktree_b = Path(claim_b.worktree)
    (worktree_a / "README.md").write_text("agent a\n")
    (worktree_b / "dest").mkdir()
    _git(worktree_b, "mv", "README.md", "dest/README.md")

    with pytest.raises(CoordinationError, match="changed file overlap"):
        coordinator.preflight(worktree_a)


def test_unrelated_branch_history_fails_closed(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "feature-a")
    worktree = Path(claim.worktree)
    tree = _git(worktree, "mktree", input="")
    unrelated = _git(worktree, "commit-tree", tree, "-m", "unrelated")
    _git(worktree, "reset", "--hard", unrelated)

    with pytest.raises(CoordinationError, match="cannot compare"):
        coordinator.preflight(worktree)


def _commit_doc(claim, relative: str, text: str) -> None:
    """Write and commit a document inside the claim worktree."""
    worktree = Path(claim.worktree)
    document = worktree / relative
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(text, encoding="utf-8")
    _git(worktree, "add", relative)
    _git(worktree, "commit", "-m", f"docs: {relative}")


ASSERTION = "El receptor de webhooks esta desplegado y responde en produccion.\n"
EVIDENCE = (
    "El receptor de webhooks esta desplegado y responde en produccion.\n\n"
    "Verificado el 2026-09-20 sobre el commit e496d54; evidencia en\n"
    "docs/operations/2026-09-20-webhook-receiver-e2e.md\n"
)


def test_state_assertion_without_evidence_blocks_review(repo: Path) -> None:
    """The gate must be able to fail. Without this, it is not a guardrail."""
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-no-evidence", paths=("docs/current-state.md",))
    _commit_doc(claim, "docs/current-state.md", ASSERTION)

    with pytest.raises(CoordinationError, match="without dated evidence"):
        coordinator._validate_state_claims_have_evidence(claim)


def test_state_assertion_with_dated_evidence_passes(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-with-evidence", paths=("docs/current-state.md",))
    _commit_doc(claim, "docs/current-state.md", EVIDENCE)

    coordinator._validate_state_claims_have_evidence(claim)


def test_conditional_sentence_is_not_a_state_assertion(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-conditional", paths=("docs/architecture.md",))
    _commit_doc(
        claim,
        "docs/architecture.md",
        "Cuando el flag automatico esta activo, el receiver responde el webhook.\n",
    )

    coordinator._validate_state_claims_have_evidence(claim)


def test_denial_is_not_a_state_assertion(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-denial", paths=("docs/operations/run.md",))
    _commit_doc(
        claim,
        "docs/operations/run.md",
        "No se desplego ningun servicio y no se activo la cohorte.\n",
    )

    coordinator._validate_state_claims_have_evidence(claim)


def test_contracts_and_design_are_exempt_by_genre(repo: Path) -> None:
    """A contract describes an interface and a design document a proposal."""
    coordinator = AgentWorkspace(repo)
    claim = _start(
        coordinator,
        repo,
        "state-exempt-genre",
        paths=("docs/contracts/c-v1.md", "docs/design/d-v1.md"),
    )
    _commit_doc(claim, "docs/contracts/c-v1.md", ASSERTION)
    _commit_doc(claim, "docs/design/d-v1.md", ASSERTION)

    coordinator._validate_state_claims_have_evidence(claim)


def test_gate_covers_a_document_no_list_mentions(repo: Path) -> None:
    """Detection is by content: a brand new file name cannot skip the gate."""
    coordinator = AgentWorkspace(repo)
    claim = _start(
        coordinator,
        repo,
        "state-unlisted-doc",
        paths=("docs/operations/nombre-que-nadie-enumero.md",),
    )
    _commit_doc(claim, "docs/operations/nombre-que-nadie-enumero.md", ASSERTION)

    with pytest.raises(CoordinationError, match="without dated evidence"):
        coordinator._validate_state_claims_have_evidence(claim)


def test_documents_untouched_by_the_branch_are_not_inspected(repo: Path) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "legacy.md").write_text(ASSERTION, encoding="utf-8")
    _git(repo, "add", "docs/legacy.md")
    _git(repo, "commit", "-m", "docs: legacy")
    _git(repo, "push", "origin", "main")

    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-untouched", paths=("src/app.py",))

    coordinator._validate_state_claims_have_evidence(claim)


def test_claim_without_base_sha_fails_closed(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-no-base", paths=("docs/current-state.md",))
    claim.data.pop("base_sha", None)

    with pytest.raises(CoordinationError, match="no base_sha"):
        coordinator._validate_state_claims_have_evidence(claim)


def test_review_transition_runs_the_evidence_gate(repo: Path) -> None:
    coordinator = AgentWorkspace(repo)
    claim = _start(coordinator, repo, "state-gate-review", paths=("docs/current-state.md",))
    coordinator.transition("state-gate-review", "implementing")
    _commit_doc(claim, "docs/current-state.md", ASSERTION)
    worktree = Path(claim.worktree)
    _git(worktree, "push", "-u", "origin", "feat/state-gate-review")

    with pytest.raises(CoordinationError, match="without dated evidence"):
        coordinator.transition("state-gate-review", "review")


def test_full_claim_commit_push_review_and_protected_push_guard(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        text=True,
        capture_output=True,
    )
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Agent")
    _git(root, "config", "user.email", "agent@example.invalid")
    (root / "README.md").write_text("fixture\n")
    scripts = root / "scripts"
    hooks = root / ".githooks"
    scripts.mkdir()
    hooks.mkdir()
    shutil.copy2(SCRIPT, scripts / "agent_workspace.py")
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\nset -eu\nroot=$(git rev-parse --show-toplevel)\n"
        'exec uv run python "$root/scripts/agent_workspace.py" '
        '--repo "$root" hook-check >/dev/null\n'
    )
    (hooks / "pre-push").write_text(
        "#!/bin/sh\nset -eu\nroot=$(git rev-parse --show-toplevel)\n"
        'exec uv run python "$root/scripts/agent_workspace.py" '
        '--repo "$root" pre-push\n'
    )
    for hook in hooks.iterdir():
        hook.chmod(0o755)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")

    coordinator = AgentWorkspace(root)
    coordinator.install_hooks()
    claim = coordinator.start(
        task_id="feature-a",
        title="Feature A",
        owner="session-a",
        branch="feat/feature-a",
        worktree=tmp_path / "feature-a",
        base_ref="origin/main",
        paths=("README.md",),
        resources=("contract:feature-a",),
    )
    worktree = Path(claim.worktree)
    coordinator.transition("feature-a", "implementing")
    (worktree / "README.md").write_text("feature\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "feat: feature a")
    _git(worktree, "push", "-u", "origin", "feat/feature-a")

    blocked = subprocess.run(
        ["git", "push", "origin", "HEAD:main"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "direct push to protected branch rejected" in blocked.stderr

    reviewed = coordinator.transition("feature-a", "review")
    assert reviewed.state == "review"
    reviewed_sha = reviewed.review_sha
    with pytest.raises(CoordinationError, match="cannot be extended"):
        coordinator.extend("feature-a", paths=("docs/after-review.md",))
    with pytest.raises(CoordinationError, match="implementing state"):
        coordinator.hook_check(worktree)

    with pytest.raises(CoordinationError, match="not contained"):
        coordinator.transition("feature-a", "merged")

    _git(worktree, "reset", "--hard", "HEAD^")
    assert coordinator.transition("feature-a", "review").review_sha == reviewed_sha
    with pytest.raises(CoordinationError, match="changed after review"):
        coordinator.transition("feature-a", "merged")

    _git(worktree, "reset", "--hard", reviewed_sha)
    _git(remote, "update-ref", "refs/heads/main", reviewed_sha)
    assert coordinator.transition("feature-a", "merged").state == "merged"
    coordinator.cleanup("feature-a")
    assert not worktree.exists()
    assert _git(root, "show-ref", "--verify", "refs/heads/feat/feature-a")
