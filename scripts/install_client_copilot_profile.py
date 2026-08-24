from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "profiles" / "client-copilot"
MANIFEST = PACKAGE / "manifest.json"
RECEIPT_NAME = "profile-package-installation.json"
AT_FDCWD = -100
RENAME_EXCHANGE = 2


def _load_manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return value


def _validated_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest file entries must be non-empty strings")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or value == RECEIPT_NAME:
        raise ValueError(f"unsafe manifest file path: {value!r}")
    return Path(*pure.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exchange_directories(left: Path, right: Path) -> None:
    """Atomically exchange two existing directories on Linux."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(left),
        AT_FDCWD,
        os.fsencode(right),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(right))


def _private_parent(target: Path, relative: Path) -> Path:
    current = target
    current.mkdir(mode=0o700, exist_ok=True)
    current.chmod(0o700)
    for part in relative.parent.parts:
        current = current / part
        if current.is_symlink():
            raise FileExistsError("target profile contains a symlink")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)
    return current


def _verify_receipted_installation(target: Path) -> None:
    receipt_path = target / RECEIPT_NAME
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise FileExistsError("target profile is not empty")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    hashes = receipt.get("sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise FileExistsError("installed profile receipt is invalid")
    for value, expected_hash in hashes.items():
        relative = _validated_relative_path(value)
        path = target / relative
        if path.is_symlink() or not path.is_file() or not isinstance(expected_hash, str):
            raise FileExistsError("installed profile has local drift")
        if _sha256(path) != expected_hash:
            raise FileExistsError("installed profile has local drift")


def install(target: Path, *, update_existing: bool = False) -> dict[str, Any]:
    manifest = _load_manifest()
    files_value = manifest.get("files")
    if not isinstance(files_value, list) or not files_value:
        raise ValueError("manifest files must be a non-empty list")
    files = [_validated_relative_path(value) for value in files_value]

    if target.is_symlink():
        raise FileExistsError("target profile must not be a symlink")
    if target.exists() and any(target.iterdir()):
        if not update_existing:
            raise FileExistsError("target profile is not empty")
        _verify_receipted_installation(target)

    sources: list[tuple[Path, Path]] = []
    for relative in files:
        source = PACKAGE / relative
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"package file missing: {relative.as_posix()}")
        sources.append((relative, source))

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
    )
    try:
        staging.chmod(0o700)
        if target.exists():
            for existing in target.rglob("*"):
                if existing.is_symlink():
                    raise FileExistsError("target profile contains a symlink")
            shutil.copytree(target, staging, dirs_exist_ok=True)
            staging.chmod(0o700)

        hashes: dict[str, str] = {}
        for relative, source in sources:
            _private_parent(staging, relative)
            destination = staging / relative
            if destination.is_symlink():
                raise FileExistsError("target profile contains a symlink")
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            hashes[relative.as_posix()] = _sha256(destination)

        receipt: dict[str, Any] = {
            "profile_name": manifest.get("profile_name"),
            "package_version": manifest.get("package_version"),
            "sha256": hashes,
        }
        receipt_path = staging / RECEIPT_NAME
        if receipt_path.is_symlink():
            raise FileExistsError("target profile contains a symlink")
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt_path.chmod(0o600)

        if target.exists():
            _exchange_directories(staging, target)
        else:
            staging.rename(target)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the versioned Client Copilot package into a profile home."
    )
    parser.add_argument("--target-home", required=True, type=Path)
    parser.add_argument("--update-existing", action="store_true")
    args = parser.parse_args()
    try:
        receipt = install(
            args.target_home.expanduser().resolve(),
            update_existing=args.update_existing,
        )
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
