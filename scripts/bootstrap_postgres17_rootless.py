#!/usr/bin/env python3
"""Download and extract a private PostgreSQL 17 Debian prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PACKAGES = (
    "postgresql-17",
    "postgresql-client-17",
    "postgresql-common",
    "postgresql-client-common",
    "libpq5",
    "libicu76",
)
REQUIRED_BINARIES = ("postgres", "initdb", "pg_ctl", "pg_isready", "psql", "createdb")


def verify_version_output(output: str) -> str:
    match = re.search(r"PostgreSQL\)\s+(\d+)\.(\d+)", output)
    if match is None or match.group(1) != "17":
        raise ValueError("PostgreSQL major 17 is required")
    return f"{match.group(1)}.{match.group(2)}"


def verify_prefix(prefix: Path) -> str:
    binary_dir = prefix / "usr/lib/postgresql/17/bin"
    for name in REQUIRED_BINARIES:
        if not (binary_dir / name).is_file():
            raise ValueError(f"missing PostgreSQL binary: {name}")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = ":".join(
        (
            str(prefix / "usr/lib/x86_64-linux-gnu"),
            str(prefix / "lib/x86_64-linux-gnu"),
        )
    )
    result = subprocess.run(
        [str(binary_dir / "postgres"), "--version"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return verify_version_output(result.stdout)


def bootstrap(output: Path) -> dict[str, object]:
    output = output.resolve()
    if output.exists():
        raise ValueError("output path already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    downloads = workspace / "downloads"
    prefix = workspace / "prefix"
    downloads.mkdir()
    try:
        subprocess.run(
            ["apt-get", "download", *PACKAGES],
            cwd=downloads,
            check=True,
            capture_output=True,
            text=True,
        )
        packages = sorted(downloads.glob("*.deb"))
        if len(packages) != len(PACKAGES):
            raise RuntimeError("resolved package count is not exact")
        prefix.mkdir()
        for package in packages:
            subprocess.run(
                ["dpkg-deb", "-x", str(package), str(prefix)],
                check=True,
                capture_output=True,
                text=True,
            )
        version = verify_prefix(prefix)
        manifest = {
            "postgres_version": version,
            "packages": [
                {
                    "filename": package.name,
                    "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                }
                for package in packages
            ],
        }
        (prefix / "bootstrap-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        shutil.rmtree(downloads)
        os.replace(prefix, output)
        return manifest
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = bootstrap(args.output)
    packages = manifest["packages"]
    if not isinstance(packages, list):
        raise RuntimeError("bootstrap manifest packages are invalid")
    print(
        f"postgres17_bootstrap=PASS version={manifest['postgres_version']} "
        f"packages={len(packages)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
