from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles" / "att1"
BUNDLE = PROFILES / "att1-product-bundle-v1.json"
EXPECTED_BUNDLE_SHA256 = "f66d30d310027f5b6483941c1dd963156732837f323189864b549fefb71f2ea6"
RECEIPT_NAME = "profile-package-installation.json"
RENAME_NOREPLACE = 1
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_regular_at(parent_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("source package integrity mismatch") from exc
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source package integrity mismatch")
        payload = _read_fd(fd)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValueError("source package integrity mismatch")
        return payload
    finally:
        os.close(fd)


def _relative_path(value: object, *, profile_name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise ValueError("bundle file paths must be non-empty strings")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != profile_name
        or pure.name == RECEIPT_NAME
    ):
        raise ValueError("bundle file path is unsafe")
    return pure.parts[1:]


def _load_bundle() -> dict[str, Any]:
    if BUNDLE.parent != PROFILES:
        raise ValueError("source package integrity mismatch")
    package_fd = _open_package_root()
    try:
        payload = _read_regular_at(package_fd, BUNDLE.name)
    finally:
        os.close(package_fd)
    if _digest(payload) != EXPECTED_BUNDLE_SHA256:
        raise ValueError("source package integrity mismatch")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("bundle manifest must be a JSON object")
    return value


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileExistsError("target parent path contains a symlink") from exc
        raise


def _open_parent_nofollow(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            try:
                next_fd = _open_child_directory(current_fd, part)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError("target parent changed during installation")
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = _open_child_directory(current_fd, part)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _assert_same_parent(path: Path, anchored_fd: int) -> None:
    current_fd = _open_parent_nofollow(path, create=False)
    try:
        anchored = os.fstat(anchored_fd)
        current = os.fstat(current_fd)
        if (anchored.st_dev, anchored.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("target parent changed during installation")
    finally:
        os.close(current_fd)


def _open_package_root() -> int:
    try:
        return _open_parent_nofollow(PROFILES, create=False)
    except OSError as exc:
        raise ValueError("source package integrity mismatch") from exc
    except RuntimeError as exc:
        raise ValueError("source package integrity mismatch") from exc


def _read_package_file(root_fd: int, profile_name: str, relative: tuple[str, ...]) -> bytes:
    current_fd = os.dup(root_fd)
    try:
        for part in (profile_name, *relative[:-1]):
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise ValueError("source package integrity mismatch") from exc
            os.close(current_fd)
            current_fd = next_fd
        try:
            file_fd = os.open(relative[-1], _FILE_FLAGS, dir_fd=current_fd)
        except OSError as exc:
            raise ValueError("source package integrity mismatch") from exc
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("source package integrity mismatch")
            payload = _read_fd(file_fd)
            after = os.fstat(file_fd)
            if (info.st_dev, info.st_ino, info.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ValueError("source package integrity mismatch")
            return payload
        finally:
            os.close(file_fd)
    finally:
        os.close(current_fd)


def _profile_payloads(
    bundle: dict[str, Any], profile_name: str
) -> tuple[dict[str, Any], list[tuple[tuple[str, ...], bytes, str]]]:
    profiles = bundle.get("profiles")
    if not isinstance(profiles, list) or profile_name not in profiles:
        raise ValueError(f"unknown profile: {profile_name}")
    entries = bundle.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("bundle files must be a non-empty list")

    package_fd = _open_package_root()
    payloads: list[tuple[tuple[str, ...], bytes, str]] = []
    seen: set[tuple[str, ...]] = set()
    try:
        for value in entries:
            if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
                raise ValueError("bundle file entries must have exact fields")
            path_value = value["path"]
            if not isinstance(path_value, str) or not path_value.startswith(f"{profile_name}/"):
                continue
            relative = _relative_path(path_value, profile_name=profile_name)
            expected_size = value["size"]
            expected_hash = value["sha256"]
            if (
                not relative
                or relative in seen
                or isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or expected_hash.lower() != expected_hash
            ):
                raise ValueError("bundle file integrity fields are invalid")
            try:
                int(expected_hash, 16)
            except ValueError as exc:
                raise ValueError("bundle file integrity fields are invalid") from exc
            payload = _read_package_file(package_fd, profile_name, relative)
            if len(payload) != expected_size or _digest(payload) != expected_hash:
                raise ValueError("source package integrity mismatch")
            seen.add(relative)
            payloads.append((relative, payload, expected_hash))
    finally:
        os.close(package_fd)
    if not payloads:
        raise ValueError("profile package has no files")
    return bundle, payloads


def _make_staging(parent_fd: int, target_name: str) -> tuple[str, int]:
    for _ in range(128):
        name = f".{target_name}.staging-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name, os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    raise FileExistsError("could not reserve a private staging directory")


def _ensure_private_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = _open_child_directory(current_fd, part)
            os.fchmod(next_fd, 0o700)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _write_private(root_fd: int, relative: tuple[str, ...], payload: bytes) -> None:
    parent_fd = _ensure_private_parent(root_fd, relative[:-1])
    try:
        fd = os.open(
            relative[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _rename_noreplace(parent_fd: int, staging_name: str, target_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(staging_name),
        parent_fd,
        os.fsencode(target_name),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError("target profile already exists")
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise RuntimeError("atomic no-replace publication is unavailable")
    raise OSError(error, os.strerror(error), target_name)


def _before_publish(_parent_fd: int, _parent_path: Path) -> None:
    """Test seam; production behavior intentionally has no action."""


def install(profile_name: str, target: Path) -> dict[str, Any]:
    bundle, payloads = _profile_payloads(_load_bundle(), profile_name)
    target = Path(os.path.abspath(os.fspath(target)))
    if target.name in {"", ".", ".."}:
        raise ValueError("target profile path is invalid")

    parent_fd = _open_parent_nofollow(target.parent, create=True)
    staging_name = ""
    try:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("target profile already exists")

        staging_name, staging_fd = _make_staging(parent_fd, target.name)
        try:
            hashes: dict[str, str] = {}
            for relative, payload, expected_hash in payloads:
                _write_private(staging_fd, relative, payload)
                hashes[PurePosixPath(*relative).as_posix()] = expected_hash
            profile_manifest = json.loads(
                next(
                    payload
                    for relative, payload, _expected_hash in payloads
                    if relative == ("manifest.json",)
                )
            )
            receipt: dict[str, Any] = {
                "bundle_name": bundle["bundle_name"],
                "bundle_version": bundle["bundle_version"],
                "bundle_sha256": EXPECTED_BUNDLE_SHA256,
                "profile_name": profile_name,
                "package_version": profile_manifest["package_version"],
                "sha256": hashes,
            }
            _write_private(
                staging_fd,
                (RECEIPT_NAME,),
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)

        _before_publish(parent_fd, target.parent)
        _assert_same_parent(target.parent, parent_fd)
        _rename_noreplace(parent_fd, staging_name, target.name)
        staging_name = ""
        _assert_same_parent(target.parent, parent_fd)
        os.fsync(parent_fd)
        return receipt
    finally:
        # Never remove by name after a failure. A concurrent same-user process can
        # replace that name between any identity check and unlink/rmdir. Retaining
        # the private 0700 staging directory is safer and makes reconciliation
        # explicit instead of risking deletion of another process's object.
        os.fsync(parent_fd)
        os.close(parent_fd)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install one versioned ATT1 product profile into an empty home."
    )
    parser.add_argument("--profile", required=True, choices=sorted(_load_bundle()["profiles"]))
    parser.add_argument("--target-home", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = install(args.profile, args.target_home.expanduser().absolute())
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
