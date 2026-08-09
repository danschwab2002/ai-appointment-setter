#!/usr/bin/env python3
"""Coordinate parallel coding agents through Git worktrees and shared claims."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ACTIVE_STATES = frozenset({"claimed", "implementing", "review"})
RESERVING_STATES = ACTIVE_STATES | {"paused"}
ALL_STATES = ACTIVE_STATES | {"paused", "merged", "abandoned"}
PROTECTED_BRANCHES = frozenset({"main", "master"})
ALLOWED_TRANSITIONS = {
    "claimed": frozenset({"implementing", "paused", "abandoned"}),
    "implementing": frozenset({"review", "paused", "abandoned"}),
    "review": frozenset({"implementing", "merged", "paused", "abandoned"}),
    "paused": frozenset({"claimed", "implementing", "abandoned"}),
    "merged": frozenset(),
    "abandoned": frozenset(),
}
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
MIGRATION_RE = re.compile(r"^supabase/migrations/(\d+)_.*\.sql$")


class CoordinationError(RuntimeError):
    """Raised when a coordination invariant would be violated."""


class Claim:
    """Typed access to one persisted task claim."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def __getattr__(self, name: str) -> Any:
        try:
            return self.data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_PREFIX",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CoordinationError(f"git {' '.join(args)} failed: {detail}")
    return result


def _git(cwd: Path, *args: str) -> str:
    return _run_git(cwd, *args).stdout.strip()


def _normalize_scope_path(value: str) -> str:
    candidate = value.strip().replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:/", candidate):
        raise CoordinationError(f"invalid repository-relative path: {value!r}")
    candidate = candidate.rstrip("/")
    path = PurePosixPath(candidate)
    if not candidate or path.is_absolute() or ".." in path.parts:
        raise CoordinationError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


class AgentWorkspace:
    """Shared coordination state for all worktrees of one Git clone."""

    def __init__(self, repo: Path | str | None = None) -> None:
        start = Path(repo or Path.cwd()).resolve()
        top = _git(start, "rev-parse", "--show-toplevel")
        self.repo = Path(top).resolve()
        common = Path(_git(self.repo, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = (self.repo / common).resolve()
        self.common_dir = common
        self.registry_dir = common / "hermes-agent-coordination"
        self.claims_dir = self.registry_dir / "claims"
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.registry_dir / "registry.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _claim_path(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(task_id):
            raise CoordinationError(
                "task_id must use 2-63 lowercase letters, digits, and hyphens"
            )
        return self.claims_dir / f"{task_id}.json"

    def _load_claims(self) -> list[Claim]:
        claims: list[Claim] = []
        for path in sorted(self.claims_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinationError(f"invalid claim registry entry: {path}") from exc
            if not isinstance(data, dict) or data.get("version") != 1:
                raise CoordinationError(f"unsupported claim registry entry: {path}")
            claims.append(Claim(data))
        return claims

    def claims(self) -> list[Claim]:
        return self._load_claims()

    def _write_claim(self, claim: Claim, *, create: bool = False) -> None:
        path = self._claim_path(claim.task_id)
        content = json.dumps(claim.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if create:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise CoordinationError(f"task already claimed: {claim.task_id}") from exc
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _worktrees(self) -> list[dict[str, str]]:
        output = _git(self.repo, "worktree", "list", "--porcelain")
        rows: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line:
                if current:
                    rows.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return rows

    def _protected_worktree_is_clean(self) -> None:
        for row in self._worktrees():
            branch = row.get("branch", "").removeprefix("refs/heads/")
            if branch not in PROTECTED_BRANCHES:
                continue
            path = Path(row["worktree"])
            if _git(path, "status", "--porcelain"):
                raise CoordinationError(f"{branch} worktree is dirty: {path}")

    def _terminal_worktrees_are_clean(self) -> None:
        for claim in self._load_claims():
            if claim.state not in {"merged", "abandoned"}:
                continue
            path = Path(claim.worktree)
            if path.is_dir() and _git(path, "status", "--porcelain"):
                raise CoordinationError(
                    f"terminal claim has a dirty worktree: {claim.task_id} ({path})"
                )

    def _reserving_worktrees_are_valid(self) -> None:
        registered = {
            Path(row["worktree"]).resolve(): row.get("branch", "").removeprefix(
                "refs/heads/"
            )
            for row in self._worktrees()
        }
        for claim in self._load_claims():
            if claim.state not in RESERVING_STATES:
                continue
            path = Path(claim.worktree)
            if not path.is_dir():
                raise CoordinationError(
                    f"reserving claim worktree is missing: {claim.task_id} ({path})"
                )
            resolved = path.resolve()
            top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
            common = Path(_git(path, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = (top / common).resolve()
            branch = _git(path, "branch", "--show-current")
            if (
                top != resolved
                or common != self.common_dir
                or branch != claim.branch
                or registered.get(resolved) != claim.branch
            ):
                raise CoordinationError(
                    f"reserving claim worktree identity mismatch: {claim.task_id}"
                )

    def _assert_unique_claim(
        self,
        *,
        branch: str,
        worktree: Path,
        paths: Sequence[str],
        resources: Sequence[str],
        state: str,
        ignore_task: str | None = None,
    ) -> None:
        if state not in RESERVING_STATES:
            return
        normalized_worktree = str(worktree.resolve())
        for existing in self._load_claims():
            if existing.task_id == ignore_task or existing.state not in RESERVING_STATES:
                continue
            if existing.branch == branch:
                raise CoordinationError(f"branch already claimed by {existing.task_id}")
            if str(Path(existing.worktree).resolve()) == normalized_worktree:
                raise CoordinationError(f"worktree already claimed by {existing.task_id}")
            shared_resources = sorted(set(resources) & set(existing.resources))
            if shared_resources:
                raise CoordinationError(
                    f"resource overlap with {existing.task_id}: {shared_resources}"
                )
            for path in paths:
                for other in existing.paths:
                    if _paths_overlap(path, other):
                        raise CoordinationError(
                            f"path overlap with {existing.task_id}: {path} <> {other}"
                        )

    def _base_sha(self, base_ref: str) -> str:
        remote, separator, branch = base_ref.partition("/")
        if (
            not separator
            or not remote
            or branch not in PROTECTED_BRANCHES
        ):
            raise CoordinationError(
                "base ref must be a remote protected branch such as origin/main"
            )
        result = _run_git(self.repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        if result.returncode != 0:
            raise CoordinationError(f"base ref does not exist: {base_ref}")
        return result.stdout.strip()

    def start(
        self,
        *,
        task_id: str,
        title: str,
        owner: str,
        branch: str,
        worktree: Path | str,
        base_ref: str = "origin/main",
        paths: Sequence[str] = (),
        resources: Sequence[str] = (),
    ) -> Claim:
        claim_path = self._claim_path(task_id)
        target = Path(worktree).resolve()
        normalized_paths = sorted({_normalize_scope_path(path) for path in paths})
        normalized_resources = sorted({resource.strip() for resource in resources if resource.strip()})
        if not normalized_paths and not normalized_resources:
            raise CoordinationError("claim requires at least one path or resource")
        if branch in PROTECTED_BRANCHES:
            raise CoordinationError("protected branch cannot own implementation work")
        if not title.strip() or not owner.strip():
            raise CoordinationError("title and owner are required")

        with self._lock():
            self._protected_worktree_is_clean()
            self._terminal_worktrees_are_clean()
            self._reserving_worktrees_are_valid()
            if claim_path.exists():
                raise CoordinationError(f"task already claimed: {task_id}")
            if target.exists():
                raise CoordinationError(f"worktree path already exists: {target}")
            if _run_git(self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
                raise CoordinationError(f"branch already exists: {branch}")
            self._assert_unique_claim(
                branch=branch,
                worktree=target,
                paths=normalized_paths,
                resources=normalized_resources,
                state="claimed",
            )
            base_sha = self._base_sha(base_ref)
            _run_git(
                self.repo,
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch,
                str(target),
                base_ref,
            )
            created = _now()
            claim = Claim(
                {
                    "version": 1,
                    "task_id": task_id,
                    "title": title.strip(),
                    "owner": owner.strip(),
                    "branch": branch,
                    "worktree": str(target),
                    "base_ref": base_ref,
                    "base_sha": base_sha,
                    "paths": normalized_paths,
                    "resources": normalized_resources,
                    "state": "claimed",
                    "created_at": created,
                    "updated_at": created,
                }
            )
            try:
                self._write_claim(claim, create=True)
            except Exception as exc:
                rollback_branch = _run_git(
                    target,
                    "branch",
                    "--show-current",
                    check=False,
                )
                rollback_head = _run_git(
                    target,
                    "rev-parse",
                    "HEAD",
                    check=False,
                )
                rollback_status = _run_git(
                    target,
                    "status",
                    "--porcelain",
                    check=False,
                )
                if (
                    rollback_branch.returncode == 0
                    and rollback_branch.stdout.strip() == branch
                    and rollback_head.returncode == 0
                    and rollback_head.stdout.strip() == base_sha
                    and rollback_status.returncode == 0
                    and not rollback_status.stdout.strip()
                ):
                    removed = _run_git(
                        self.repo,
                        "worktree",
                        "remove",
                        str(target),
                        check=False,
                    )
                    if removed.returncode == 0:
                        _run_git(
                            self.repo,
                            "update-ref",
                            "-d",
                            f"refs/heads/{branch}",
                            base_sha,
                            check=False,
                        )
                if target.exists():
                    raise CoordinationError(
                        f"claim persistence failed; worktree preserved for recovery: {target}"
                    ) from exc
                raise CoordinationError(
                    "claim persistence failed; clean worktree rollback completed"
                ) from exc
            return claim

    def adopt(
        self,
        *,
        task_id: str,
        title: str,
        owner: str,
        worktree: Path | str,
        base_ref: str = "origin/main",
        paths: Sequence[str] = (),
        resources: Sequence[str] = (),
        state: str = "paused",
    ) -> Claim:
        if state not in {"claimed", "implementing", "paused"}:
            raise CoordinationError(
                "adopt state must be claimed, implementing, or paused"
            )
        if not title.strip() or not owner.strip():
            raise CoordinationError("title and owner are required")
        target = Path(worktree).resolve()
        target_top = Path(_git(target, "rev-parse", "--show-toplevel")).resolve()
        target_common = Path(_git(target_top, "rev-parse", "--git-common-dir"))
        if not target_common.is_absolute():
            target_common = (target_top / target_common).resolve()
        if target_common != self.common_dir:
            raise CoordinationError("worktree belongs to a different Git clone")
        branch = _git(target, "branch", "--show-current")
        if not branch:
            raise CoordinationError("detached worktree cannot be adopted")
        normalized_paths = sorted({_normalize_scope_path(path) for path in paths})
        normalized_resources = sorted({resource.strip() for resource in resources if resource.strip()})
        if not normalized_paths and not normalized_resources:
            raise CoordinationError("claim requires at least one path or resource")
        with self._lock():
            if self._claim_path(task_id).exists():
                raise CoordinationError(f"task already claimed: {task_id}")
            self._assert_unique_claim(
                branch=branch,
                worktree=target,
                paths=normalized_paths,
                resources=normalized_resources,
                state=state,
            )
            created = _now()
            claim = Claim(
                {
                    "version": 1,
                    "task_id": task_id,
                    "title": title.strip(),
                    "owner": owner.strip(),
                    "branch": branch,
                    "worktree": str(target),
                    "base_ref": base_ref,
                    "base_sha": self._base_sha(base_ref),
                    "paths": normalized_paths,
                    "resources": normalized_resources,
                    "state": state,
                    "created_at": created,
                    "updated_at": created,
                }
            )
            self._write_claim(claim, create=True)
            return claim

    def _claim_for_worktree(self, cwd: Path) -> Claim | None:
        path = Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve()
        branch = _git(path, "branch", "--show-current")
        for claim in self._load_claims():
            if (
                Path(claim.worktree).resolve() == path
                and claim.branch == branch
                and claim.state in ACTIVE_STATES
            ):
                return claim
        return None

    def _changed_paths(self, claim: Claim) -> set[str]:
        worktree = Path(claim.worktree)
        if not worktree.is_dir():
            raise CoordinationError(
                f"claim worktree is missing: {claim.task_id} ({worktree})"
            )
        changed: set[str] = set()
        committed = _run_git(
            worktree,
            "diff",
            "--name-status",
            "-z",
            f"{claim.base_sha}...HEAD",
            check=False,
        )
        if committed.returncode != 0:
            detail = (committed.stderr or committed.stdout).strip()
            raise CoordinationError(
                f"cannot compare {claim.task_id} with its base: {detail}"
            )
        committed_fields = committed.stdout.split("\0")
        index = 0
        while index < len(committed_fields) - 1:
            status = committed_fields[index]
            index += 1
            if status.startswith(("R", "C")):
                changed.add(committed_fields[index])
                changed.add(committed_fields[index + 1])
                index += 2
            else:
                changed.add(committed_fields[index])
                index += 1
        status = _run_git(
            worktree,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout
        status_fields = status.split("\0")
        index = 0
        while index < len(status_fields) - 1:
            record = status_fields[index]
            index += 1
            code = record[:2]
            value = record[3:]
            if value:
                changed.add(value)
            if "R" in code or "C" in code:
                source = status_fields[index]
                index += 1
                if source:
                    changed.add(source)
        return changed

    def _assert_actual_changes_do_not_overlap(self) -> None:
        active = [claim for claim in self._load_claims() if claim.state in ACTIVE_STATES]
        changes = {claim.task_id: self._changed_paths(claim) for claim in active}
        for index, left in enumerate(active):
            for right in active[index + 1 :]:
                shared = sorted(changes[left.task_id] & changes[right.task_id])
                if shared:
                    raise CoordinationError(
                        f"changed file overlap between {left.task_id} and {right.task_id}: {shared}"
                    )
                left_migrations = {
                    match.group(1): path
                    for path in changes[left.task_id]
                    if (match := MIGRATION_RE.fullmatch(path))
                }
                right_migrations = {
                    match.group(1): path
                    for path in changes[right.task_id]
                    if (match := MIGRATION_RE.fullmatch(path))
                }
                versions = sorted(set(left_migrations) & set(right_migrations))
                if versions:
                    raise CoordinationError(
                        "migration version overlap between "
                        f"{left.task_id} and {right.task_id}: {versions}"
                    )

    def preflight(self, cwd: Path | str | None = None) -> Claim:
        path = Path(cwd or Path.cwd()).resolve()
        branch = _git(path, "branch", "--show-current")
        if branch in PROTECTED_BRANCHES:
            raise CoordinationError(f"protected branch cannot be used for implementation: {branch}")
        claim = self._claim_for_worktree(path)
        if claim is None:
            raise CoordinationError("no active claim matches this branch and worktree")
        self._protected_worktree_is_clean()
        self._reserving_worktrees_are_valid()
        self._assert_actual_changes_do_not_overlap()
        changed = self._changed_paths(claim)
        if claim.paths:
            outside = sorted(
                changed_path
                for changed_path in changed
                if not any(_paths_overlap(changed_path, scope) for scope in claim.paths)
            )
            if outside:
                raise CoordinationError(
                    f"changed paths outside declared scope for {claim.task_id}: {outside}"
                )
        return claim

    def _verified_remote_head(self, worktree: Path, base_ref: str) -> str:
        remote, separator, branch = base_ref.partition("/")
        if not separator or not remote or not branch:
            raise CoordinationError(
                f"merged verification requires a remote branch ref: {base_ref}"
            )
        remote_ref = f"refs/heads/{branch}"
        advertised = _run_git(
            worktree,
            "ls-remote",
            "--exit-code",
            "--refs",
            remote,
            remote_ref,
            check=False,
        )
        if advertised.returncode != 0:
            detail = (advertised.stderr or advertised.stdout).strip()
            raise CoordinationError(
                f"cannot verify protected remote ref {base_ref}: {detail}"
            )
        fields = advertised.stdout.strip().split()
        if len(fields) != 2 or fields[1] != remote_ref:
            raise CoordinationError(f"unexpected remote ref response for {base_ref}")
        advertised_sha = fields[0]
        fetched = _run_git(
            worktree,
            "fetch",
            "--quiet",
            remote,
            remote_ref,
            check=False,
        )
        if fetched.returncode != 0:
            detail = (fetched.stderr or fetched.stdout).strip()
            raise CoordinationError(
                f"cannot fetch protected remote ref {base_ref}: {detail}"
            )
        fetched_sha = _git(worktree, "rev-parse", "FETCH_HEAD")
        if fetched_sha != advertised_sha:
            raise CoordinationError(
                f"protected remote ref changed during verification: {base_ref}"
            )
        return fetched_sha

    def transition(self, task_id: str, state: str) -> Claim:
        if state not in ALL_STATES:
            raise CoordinationError(f"invalid claim state: {state}")
        with self._lock():
            claims = {claim.task_id: claim for claim in self._load_claims()}
            claim = claims.get(task_id)
            if claim is None:
                raise CoordinationError(f"unknown task: {task_id}")
            if state == claim.state:
                return claim
            if state != claim.state and state not in ALLOWED_TRANSITIONS[claim.state]:
                if claim.state in {"merged", "abandoned"}:
                    raise CoordinationError(
                        f"terminal claim cannot be reopened: {task_id} ({claim.state})"
                    )
                raise CoordinationError(
                    f"invalid state transition for {task_id}: {claim.state} -> {state}"
                )
            if state in ACTIVE_STATES:
                self._assert_unique_claim(
                    branch=claim.branch,
                    worktree=Path(claim.worktree),
                    paths=claim.paths,
                    resources=claim.resources,
                    state=state,
                    ignore_task=task_id,
                )
            if state == "review":
                worktree = Path(claim.worktree)
                self.preflight(worktree)
                if _git(worktree, "status", "--porcelain"):
                    raise CoordinationError("worktree is dirty; commit before review")
                upstream = _run_git(
                    worktree,
                    "rev-parse",
                    "--abbrev-ref",
                    "--symbolic-full-name",
                    "@{upstream}",
                    check=False,
                )
                if upstream.returncode != 0:
                    raise CoordinationError("branch is not published")
                upstream_ref = upstream.stdout.strip()
                _, separator, upstream_branch = upstream_ref.partition("/")
                if not separator or upstream_branch != claim.branch:
                    raise CoordinationError(
                        "upstream does not match the claimed feature branch"
                    )
                divergence = _git(worktree, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
                if divergence != "0\t0":
                    raise CoordinationError(f"branch is not synchronized with upstream: {divergence}")
                review_sha = _git(worktree, "rev-parse", "HEAD")
                remote_feature_head = self._verified_remote_head(
                    worktree,
                    upstream_ref,
                )
                if remote_feature_head != review_sha:
                    raise CoordinationError(
                        "live remote feature branch does not match local HEAD"
                    )
                claim.data["review_sha"] = review_sha
            if state == "merged":
                worktree = Path(claim.worktree)
                if _git(worktree, "status", "--porcelain"):
                    raise CoordinationError("worktree is dirty; cannot mark merged")
                review_sha = claim.data.get("review_sha")
                if not review_sha:
                    raise CoordinationError("claim has no verified review commit")
                current_sha = _git(worktree, "rev-parse", "HEAD")
                if current_sha != review_sha:
                    raise CoordinationError(
                        "branch HEAD changed after review; return to implementing"
                    )
                remote_head = self._verified_remote_head(worktree, claim.base_ref)
                contained = _run_git(
                    worktree,
                    "merge-base",
                    "--is-ancestor",
                    review_sha,
                    remote_head,
                    check=False,
                )
                if contained.returncode != 0:
                    raise CoordinationError(
                        f"branch HEAD is not contained in {claim.base_ref}; verify remote merge first"
                    )
            if state == "abandoned":
                worktree = Path(claim.worktree)
                if worktree.is_dir() and _git(worktree, "status", "--porcelain"):
                    raise CoordinationError(
                        "worktree is dirty; use paused until work is preserved or cleaned"
                    )
            if state == "implementing":
                claim.data.pop("review_sha", None)
            claim.data["state"] = state
            claim.data["updated_at"] = _now()
            self._write_claim(claim)
            return claim

    def extend(
        self,
        task_id: str,
        *,
        paths: Sequence[str] = (),
        resources: Sequence[str] = (),
    ) -> Claim:
        """Atomically extend an existing claim without weakening its scope."""
        additions = {_normalize_scope_path(path) for path in paths}
        resource_additions = {resource.strip() for resource in resources if resource.strip()}
        if not additions and not resource_additions:
            raise CoordinationError("extend requires at least one path or resource")
        with self._lock():
            claims = {claim.task_id: claim for claim in self._load_claims()}
            claim = claims.get(task_id)
            if claim is None:
                raise CoordinationError(f"unknown task: {task_id}")
            if claim.state not in {"claimed", "implementing", "paused"}:
                raise CoordinationError(
                    f"claim cannot be extended in state: {task_id} ({claim.state})"
                )
            combined_paths = sorted(set(claim.paths) | additions)
            combined_resources = sorted(set(claim.resources) | resource_additions)
            self._assert_unique_claim(
                branch=claim.branch,
                worktree=Path(claim.worktree),
                paths=combined_paths,
                resources=combined_resources,
                state=claim.state,
                ignore_task=task_id,
            )
            claim.data["paths"] = combined_paths
            claim.data["resources"] = combined_resources
            claim.data["updated_at"] = _now()
            self._write_claim(claim)
            return claim

    def cleanup(self, task_id: str) -> Claim:
        """Remove a clean terminal worktree without force or history loss."""
        with self._lock():
            claims = {claim.task_id: claim for claim in self._load_claims()}
            claim = claims.get(task_id)
            if claim is None:
                raise CoordinationError(f"unknown task: {task_id}")
            if claim.state not in {"merged", "abandoned"}:
                raise CoordinationError(
                    f"cleanup requires a terminal claim: {task_id} ({claim.state})"
                )
            worktree = Path(claim.worktree)
            if not worktree.is_dir():
                raise CoordinationError(f"worktree is already absent: {worktree}")
            top = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
            common = Path(_git(worktree, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = (top / common).resolve()
            branch = _git(worktree, "branch", "--show-current")
            if top != worktree.resolve() or common != self.common_dir or branch != claim.branch:
                raise CoordinationError(
                    "worktree identity no longer matches the terminal claim"
                )
            if _git(worktree, "status", "--porcelain"):
                raise CoordinationError("worktree is dirty; cleanup rejected")
            _run_git(self.repo, "worktree", "remove", str(worktree))
            claim.data["worktree_removed_at"] = _now()
            claim.data["updated_at"] = _now()
            self._write_claim(claim)
            return claim

    def install_hooks(self) -> None:
        hooks_dir = self.repo / ".githooks"
        required = [hooks_dir / "pre-commit", hooks_dir / "pre-push"]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise CoordinationError(f"versioned hooks are missing: {missing}")
        not_executable = [str(path) for path in required if not os.access(path, os.X_OK)]
        if not_executable:
            raise CoordinationError(
                f"versioned hooks are not executable: {not_executable}"
            )
        _run_git(self.repo, "config", "core.hooksPath", ".githooks")

    def validate_tree(self) -> dict[str, Any]:
        """Validate repository invariants that do not require local claim state."""
        migration_dir = self.repo / "supabase" / "migrations"
        by_version: dict[str, list[str]] = {}
        if migration_dir.is_dir():
            for path in sorted(migration_dir.glob("*.sql")):
                relative = path.relative_to(self.repo).as_posix()
                match = MIGRATION_RE.fullmatch(relative)
                if match:
                    by_version.setdefault(match.group(1), []).append(relative)
        duplicates = {
            version: paths for version, paths in by_version.items() if len(paths) > 1
        }
        if duplicates:
            raise CoordinationError(f"duplicate migration versions: {duplicates}")
        return {
            "migration_files": sum(len(paths) for paths in by_version.values()),
            "duplicate_migration_versions": 0,
        }

    def hook_check(self, cwd: Path | str | None = None) -> Claim | None:
        path = Path(cwd or Path.cwd()).resolve()
        branch = _git(path, "branch", "--show-current")
        if branch in PROTECTED_BRANCHES:
            if os.environ.get("HERMES_INTEGRATOR") == "1":
                return None
            raise CoordinationError(
                f"protected branch commit/push rejected: {branch}; use a claimed worktree"
            )
        claim = self.preflight(path)
        if claim.state != "implementing":
            raise CoordinationError(
                f"commit/push requires implementing state: {claim.task_id} ({claim.state})"
            )
        return claim

    def check_push_input(self, lines: Sequence[str]) -> None:
        if os.environ.get("HERMES_INTEGRATOR") == "1":
            return
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            remote_ref = fields[2]
            if remote_ref in {"refs/heads/main", "refs/heads/master"}:
                raise CoordinationError(
                    f"direct push to protected branch rejected: {remote_ref}"
                )

    def status(self) -> list[dict[str, Any]]:
        claims_by_path = {
            str(Path(claim.worktree).resolve()): claim for claim in self._load_claims()
        }
        rows: list[dict[str, Any]] = []
        for worktree in self._worktrees():
            path = str(Path(worktree["worktree"]).resolve())
            branch = worktree.get("branch", "").removeprefix("refs/heads/")
            claim = claims_by_path.get(path)
            if branch in PROTECTED_BRANCHES:
                management = "protected"
            elif claim is not None:
                management = "claimed"
            else:
                management = "unmanaged"
            rows.append(
                {
                    "worktree": path,
                    "branch": branch,
                    "management": management,
                    "task_id": claim.task_id if claim else None,
                    "state": claim.state if claim else None,
                    "dirty": bool(_git(Path(path), "status", "--porcelain")),
                }
            )
        return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("task_id")
    start.add_argument("--title", required=True)
    start.add_argument("--owner", required=True)
    start.add_argument("--branch", required=True)
    start.add_argument("--worktree", type=Path, required=True)
    start.add_argument("--base", default="origin/main")
    start.add_argument("--path", action="append", default=[])
    start.add_argument("--resource", action="append", default=[])

    adopt = sub.add_parser("adopt")
    adopt.add_argument("task_id")
    adopt.add_argument("--title", required=True)
    adopt.add_argument("--owner", required=True)
    adopt.add_argument("--worktree", type=Path, required=True)
    adopt.add_argument("--base", default="origin/main")
    adopt.add_argument("--path", action="append", default=[])
    adopt.add_argument("--resource", action="append", default=[])
    adopt.add_argument(
        "--state",
        choices=["claimed", "implementing", "paused"],
        default="paused",
    )

    sub.add_parser("preflight")
    sub.add_parser("status")
    transition = sub.add_parser("transition")
    transition.add_argument("task_id")
    transition.add_argument("state", choices=sorted(ALL_STATES))
    extend = sub.add_parser("extend")
    extend.add_argument("task_id")
    extend.add_argument("--path", action="append", default=[])
    extend.add_argument("--resource", action="append", default=[])
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("task_id")
    sub.add_parser("hook-check")
    sub.add_parser("pre-push")
    sub.add_parser("install-hooks")
    sub.add_parser("validate-tree")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        coordinator = AgentWorkspace(args.repo)
        if args.command == "start":
            result: Any = coordinator.start(
                task_id=args.task_id,
                title=args.title,
                owner=args.owner,
                branch=args.branch,
                worktree=args.worktree,
                base_ref=args.base,
                paths=args.path,
                resources=args.resource,
            ).to_dict()
        elif args.command == "adopt":
            result = coordinator.adopt(
                task_id=args.task_id,
                title=args.title,
                owner=args.owner,
                worktree=args.worktree,
                base_ref=args.base,
                paths=args.path,
                resources=args.resource,
                state=args.state,
            ).to_dict()
        elif args.command == "preflight":
            result = coordinator.preflight(args.repo).to_dict()
        elif args.command == "status":
            result = coordinator.status()
        elif args.command == "transition":
            result = coordinator.transition(args.task_id, args.state).to_dict()
        elif args.command == "extend":
            result = coordinator.extend(
                args.task_id,
                paths=args.path,
                resources=args.resource,
            ).to_dict()
        elif args.command == "cleanup":
            result = coordinator.cleanup(args.task_id).to_dict()
        elif args.command == "hook-check":
            claim = coordinator.hook_check(args.repo)
            result = claim.to_dict() if claim else {"integrator_override": True}
        elif args.command == "pre-push":
            coordinator.hook_check(args.repo)
            coordinator.check_push_input(sys.stdin.read().splitlines())
            result = {"push_guard": "ok"}
        elif args.command == "install-hooks":
            coordinator.install_hooks()
            result = {"hooks_path": ".githooks"}
        elif args.command == "validate-tree":
            result = coordinator.validate_tree()
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except CoordinationError as exc:
        print(f"coordination_error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
