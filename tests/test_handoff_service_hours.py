"""E-486 (v1.37.0, item 1e y D1): el horario del equipo en el aviso al derivar."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from bridge.handoff_notice import (
    HANDOFF_NOTICE_TEXT,
    ServiceHours,
    default_service_hours,
    handoff_notice_text,
)
from bridge.instance_manifest import InstanceManifest, ManifestError

ROOT = Path(__file__).resolve().parents[1]
ATT1 = ROOT / "tests" / "fixtures" / "instances" / "att1" / "instancia.toml"

CDMX = default_service_hours("America/Mexico_City")
# Martes 10/11 11:00 de CDMX (17:00Z) y viernes 13/11 20:00 (sabado 02:00Z).
TUESDAY_11 = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
FRIDAY_20 = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
SATURDAY_11 = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)


def test_the_default_is_nine_to_eight_weekdays_in_the_instance_zone() -> None:
    assert CDMX.days == (0, 1, 2, 3, 4)
    assert (CDMX.start, CDMX.end) == (time(9, 0), time(20, 0))
    assert CDMX.city == "Ciudad de México"


def test_inside_hours_only_the_fixed_sentence() -> None:
    assert handoff_notice_text(CDMX, TUESDAY_11) == HANDOFF_NOTICE_TEXT


@pytest.mark.parametrize("now", [FRIDAY_20, SATURDAY_11])
def test_outside_hours_the_second_sentence(now: datetime) -> None:
    assert handoff_notice_text(CDMX, now) == (
        f"{HANDOFF_NOTICE_TEXT} El equipo atiende de lunes a viernes de "
        "09:00 a 20:00 (hora de Ciudad de México)."
    )


def test_without_hours_never_the_second_sentence() -> None:
    assert handoff_notice_text(None, FRIDAY_20) == HANDOFF_NOTICE_TEXT


@pytest.mark.parametrize(
    ("days", "text"),
    [
        ((0, 1, 2, 3, 4, 5, 6), "todos los días"),
        ((5,), "los sábado"),
        ((0, 2, 4), "los lunes, miércoles y viernes"),
        ((0, 1, 2, 3, 4, 5), "de lunes a sábado"),
    ],
)
def test_days_read_naturally(days: tuple[int, ...], text: str) -> None:
    assert replace(CDMX, days=days).describe_days() == text


def _manifest(extra: str) -> InstanceManifest:
    payload = tomllib.loads(ATT1.read_text(encoding="utf-8") + "\n" + extra)
    return InstanceManifest.from_mapping(payload)


def test_a_manifest_without_atencion_uses_the_default() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1)
    assert manifest.attention is None
    assert manifest.service_hours == default_service_hours(manifest.time_zone)


def test_a_declared_atencion_is_read() -> None:
    manifest = _manifest(
        '[atencion]\ndias = ["lunes", "Miercoles", "sábado"]\n'
        'desde = "10:30"\nhasta = "18:00"\nciudad = "CDMX"\n'
    )
    assert manifest.service_hours == ServiceHours(
        days=(0, 2, 5),
        start=time(10, 30),
        end=time(18, 0),
        time_zone=manifest.time_zone,
        city="CDMX",
    )


@pytest.mark.parametrize(
    "block",
    [
        '[atencion]\ndias = ["lunes"]\ndesde = "9:00"\nhasta = "20:00"\n',
        '[atencion]\ndias = ["feriado"]\ndesde = "09:00"\nhasta = "20:00"\n',
        '[atencion]\ndias = []\ndesde = "09:00"\nhasta = "20:00"\n',
        '[atencion]\ndias = ["lunes"]\ndesde = "20:00"\nhasta = "09:00"\n',
        '[atencion]\ndias = ["lunes", "lunes"]\ndesde = "09:00"\nhasta = "20:00"\n',
        '[atencion]\ndias = ["lunes"]\ndesde = "09:00"\nhasta = "20:00"\notro = 1\n',
    ],
)
def test_a_malformed_atencion_does_not_load(block: str) -> None:
    with pytest.raises(ManifestError, match="atencion"):
        _manifest(block)
