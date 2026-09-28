"""La version del producto es una sola: pyproject, CHANGELOG y el workflow de release.

El workflow `release.yml` rechaza un tag que no coincida con `pyproject.toml` o que no
tenga entrada en `CHANGELOG.md`. Este test adelanta ese rechazo al PR, para que el
tag no se descubra roto recien al publicarlo.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _changelog_versions() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text()
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)


def test_project_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _project_version())


def test_changelog_top_entry_matches_project_version() -> None:
    versions = _changelog_versions()
    assert versions, "CHANGELOG.md no tiene ninguna entrada ## [X.Y.Z]"
    assert versions[0] == _project_version()


def test_changelog_versions_are_unique_and_descending() -> None:
    versions = _changelog_versions()
    keys = [tuple(int(part) for part in v.split(".")) for v in versions]
    assert len(set(keys)) == len(keys)
    assert keys == sorted(keys, reverse=True)


def test_lockfile_records_project_version() -> None:
    lock = (ROOT / "uv.lock").read_text()
    match = re.search(
        r'\[\[package\]\]\nname = "ai-appointment-setter"\nversion = "([^"]+)"', lock
    )
    assert match, "uv.lock no registra el paquete del proyecto"
    assert match.group(1) == _project_version()


def test_every_image_dockerfile_bakes_the_release_identity() -> None:
    for dockerfile in (
        "Dockerfile",
        "deploy/slack-connector.Dockerfile",
        "deploy/daily-feedback.Dockerfile",
    ):
        text = (ROOT / dockerfile).read_text()
        assert "ARG SETTER_VERSION" in text, dockerfile
        assert "ARG GIT_SHA" in text, dockerfile
        assert 'SETTER_VERSION="${SETTER_VERSION}"' in text, dockerfile


def test_release_workflow_publishes_the_three_images() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    for image in ("setter-bridge", "setter-slack-connector", "setter-daily-feedback"):
        assert f"image: {image}" in workflow
    assert "merge-base --is-ancestor" in workflow
