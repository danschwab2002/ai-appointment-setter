"""Every *_ENABLED flag the bridge reads is listed in an env example.

T3 (2026-11-28): 38 flags of src/bridge were missing from .env.example (and
the deploy/*.env.example files), so whoever set up an instance from the example
did not know they existed. This test crosses the "..._ENABLED" string literals
of src/bridge against the keys of the env examples, so a new flag cannot be
merged without its line.

Run it alone to see the list: uv run pytest tests/test_env_example_flags.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAG_LITERAL = re.compile(r"[\"']([A-Z][A-Z0-9_]*_ENABLED)[\"']")
ENV_KEY = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def bridge_flags() -> set[str]:
    flags: set[str] = set()
    for path in sorted((ROOT / "src" / "bridge").rglob("*.py")):
        flags.update(FLAG_LITERAL.findall(path.read_text(encoding="utf-8")))
    return flags


def env_example_keys() -> set[str]:
    keys: set[str] = set()
    examples = [ROOT / ".env.example", *sorted((ROOT / "deploy").glob("*.env.example"))]
    for path in examples:
        keys.update(ENV_KEY.findall(path.read_text(encoding="utf-8")))
    return keys


def test_bridge_reads_flags_at_all() -> None:
    # Guard against a regex that matches nothing and turns the next test green.
    assert len(bridge_flags()) > 100


def test_every_bridge_flag_is_in_an_env_example() -> None:
    missing = sorted(bridge_flags() - env_example_keys())
    assert missing == [], (
        "flags read by src/bridge without a line in .env.example or "
        "deploy/*.env.example (add each one, off, with what it does): "
        + ", ".join(missing)
    )


def test_main_env_example_has_no_duplicate_keys() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert duplicates == []
