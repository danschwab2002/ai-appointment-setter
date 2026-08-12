#!/usr/bin/env python3
"""Build a commit-pinned, non-executing Supabase release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PREFIX_LAST = "20260808000300"
REQUIRED_POSTFLIGHT = (
    "scripts/supabase_schema_inventory.sql",
    "scripts/supabase_acl_inventory.sql",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def migration_records(repo: Path) -> list[dict[str, str]]:
    files = sorted((repo / "supabase" / "migrations").glob("*.sql"))
    if not files:
        raise ValueError("canonical migration stack is empty")
    records = [
        {
            "version": path.name.split("_", 1)[0],
            "filename": path.name,
            "sha256": sha256(path),
        }
        for path in files
    ]
    versions = [record["version"] for record in records]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration version")
    if PREFIX_LAST not in versions:
        raise ValueError("proven prefix boundary is absent")
    return records


def build(repo: Path, output: Path, *, allow_dirty: bool = False) -> dict[str, object]:
    repo = repo.resolve()
    output = output.resolve()
    commit = git(repo, "rev-parse", "HEAD")
    dirty = bool(git(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise ValueError("repository is dirty; freeze and commit before bundling")

    records = migration_records(repo)
    boundary = next(i for i, row in enumerate(records) if row["version"] == PREFIX_LAST)
    prefix = records[: boundary + 1]
    tail = records[boundary + 1 :]
    if not tail:
        raise ValueError("pending tail is empty")

    migration_sources = [
        repo / "supabase" / "migrations" / row["filename"] for row in tail
    ]
    postflight_sources = [repo / relative for relative in REQUIRED_POSTFLIGHT]
    for source in (*migration_sources, *postflight_sources):
        if not source.is_file():
            raise ValueError("required bundle artifact missing")
    if output.exists():
        raise ValueError("output path already exists")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        bundle_path = staging / "pending-tail.sql"
        with bundle_path.open("wb") as target:
            for row, source in zip(tail, migration_sources, strict=True):
                content = source.read_bytes()
                target.write(f"\n-- BEGIN {row['filename']}\n".encode())
                target.write(content)
                if not content.endswith(b"\n"):
                    target.write(b"\n")
                target.write(f"-- END {row['filename']}\n".encode())

        postflight_path = staging / "postflight.sql"
        with postflight_path.open("wb") as target:
            for relative, source in zip(
                REQUIRED_POSTFLIGHT, postflight_sources, strict=True
            ):
                content = source.read_bytes()
                target.write(f"\n-- BEGIN {relative}\n".encode())
                target.write(content)
                if not content.endswith(b"\n"):
                    target.write(b"\n")
                target.write(f"-- END {relative}\n".encode())

        manifest: dict[str, object] = {
            "format": "supabase_release_bundle/v1",
            "commit": commit,
            "dirty_source": dirty,
            "prefix_last": PREFIX_LAST,
            "prefix": prefix,
            "pending_tail": tail,
            "bundle": {"filename": bundle_path.name, "sha256": sha256(bundle_path)},
            "postflight": {
                "filename": postflight_path.name,
                "sha256": sha256(postflight_path),
            },
            "production_authorized": False,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(staging, output)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build(args.repo, args.output)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"bundle_status=blocked reason={type(exc).__name__}", file=sys.stderr)
        return 2
    pending_tail = manifest["pending_tail"]
    if not isinstance(pending_tail, list):
        print("bundle_status=blocked reason=invalid_manifest", file=sys.stderr)
        return 2
    print(
        "bundle_status=prepared "
        f"commit={manifest['commit']} "
        f"pending_count={len(pending_tail)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
