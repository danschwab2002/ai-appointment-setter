"""El doble caso del carrito, del lado del bridge (E-204, M1a, P12 del mapa de M16).

La base (migracion 20261030000100, validate_flow_engine_native_legacy_double_case.mjs):

* (a) la admision nativa de un carrito no arranca si la persona tiene un caso
  del legado abierto del producto, de cualquier oferta:
  ``motor_no_arranca`` con ``caso_del_legado_en_curso``;
* (b) el planificador del legado aborta con 55000 ``pilot_scope_rejected`` /
  ``flujo_nativo_en_curso`` si la persona tiene un caso de un carrito nativo
  abierto (la vuelta atras: el nativo pausado o apagado).

Lo que fija este archivo: (a) es un motivo «sin reclamo», asi que el carrito
vuelve al legado y el legado hace exactamente lo de hoy (D4 del mapa, por
default), y no queda ``failed flow_engine_no_start``; (b) deja el evento
``failed`` con el motivo del legado, sin mensaje y sin reintento. Las filas son
las que contesta la base en PGlite
(``tests/fixtures/flow_engine/native_legacy_double_case_rows.json``), no
escritas a mano.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_flow_engine_native_cart import _answer, _NativeBase, _resolve_native, _row
from test_flow_engine_resolution import _Base, _resolve

from bridge.native_flows import NATIVE_CART_UNCLAIMED_REASONS
from bridge.resolution import ResolutionError

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads(
    (FIXTURES / "flow_engine" / "native_legacy_double_case_rows.json").read_text(encoding="utf-8")
)
PLANNER = "plan_lancemos_pilot_cart_recovery"


def _postgrest_error(row: dict[str, Any]) -> httpx.Response:
    """El error como lo devuelve PostgREST: code, message, details (y hint)."""
    return httpx.Response(400, json={"code": row["code"], "message": row["message"],
                                     "details": row["details"], "hint": None})


class _PlannerAnswers:
    """Que el planificador del legado conteste lo que dijo la base en PGlite."""

    def __init__(self, base: _Base, plan: httpx.Response | None) -> None:
        self.plan = plan
        inner = base.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if self.plan is not None and request.url.path == f"/rest/v1/rpc/{PLANNER}":
                base.calls.append((PLANNER, json.loads(request.content)))
                return self.plan
            return inner(request)

        base.handler = handler  # type: ignore[method-assign]


def test_the_rows_are_the_ones_of_the_database() -> None:
    assert ROWS["_capture"]["encargo"] == "E-204"
    assert ROWS["admission"] == {"outcome": "motor_no_arranca", "reason": "caso_del_legado_en_curso",
                                 "created": None}
    assert ROWS["legacy_same_offer"]["message"] == "pilot_case_binding_conflict"
    assert (ROWS["legacy_over_native"]["message"], ROWS["legacy_over_native"]["details"]) == (
        "pilot_scope_rejected", "flujo_nativo_en_curso")


def test_an_open_legacy_case_leaves_the_cart_to_the_legacy() -> None:
    assert ROWS["admission"]["reason"] in NATIVE_CART_UNCLAIMED_REASONS


def test_with_an_open_legacy_case_the_legacy_plans_exactly_as_today() -> None:
    # Hoy, sin flujo nativo del carrito: el legado planifica el carrito.
    today = _Base()
    _resolve(today, flow_engine=True)
    base = _NativeBase([_answer(_row(ROWS["admission"]["outcome"], ROWS["admission"]["reason"]))])
    _resolve_native(base)

    assert base.body_of(PLANNER) == today.body_of(PLANNER)
    assert [name for name in base.names() if name != "admit_flow_engine_event_v1"] == today.names()
    assert base.status_patches == today.status_patches


def test_with_the_same_offer_the_legacy_says_what_it_says_today() -> None:
    # La misma oferta: el piloto aborta con el binding del caso abierto, como sin el nativo.
    today = _Base()
    _PlannerAnswers(today, _postgrest_error(ROWS["legacy_same_offer"]))
    with pytest.raises(ResolutionError, match="^pilot_case_binding_conflict$"):
        _resolve(today, flow_engine=True)
    base = _NativeBase([_answer(_row(ROWS["admission"]["outcome"], ROWS["admission"]["reason"]))])
    _PlannerAnswers(base, _postgrest_error(ROWS["legacy_same_offer"]))
    with pytest.raises(ResolutionError, match="^pilot_case_binding_conflict$"):
        _resolve_native(base)

    assert base.status_patches == today.status_patches == [
        {"processing_status": "failed", "processed_at": None, "processing_error": "pilot_case_binding_conflict"}
    ]


def test_red_without_the_reason_the_event_would_end_without_the_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rojo: si el motivo no fuera «sin reclamo», el evento quedaria failed sin el legado.
    from bridge import native_flows

    monkeypatch.setattr(native_flows, "NATIVE_CART_UNCLAIMED_REASONS",
                        native_flows.NATIVE_CART_UNCLAIMED_REASONS - {ROWS["admission"]["reason"]})
    base = _NativeBase([_answer(_row(ROWS["admission"]["outcome"], ROWS["admission"]["reason"]))])
    with pytest.raises(ResolutionError, match="^flow_engine_no_start:caso_del_legado_en_curso$"):
        _resolve_native(base)
    assert not any(name.startswith("plan_") for name in base.names())


def test_over_an_open_native_cart_case_the_legacy_ends_the_event_without_a_message() -> None:
    # La vuelta atras: el nativo pausado (flujo_pausado, sin reclamo) y su caso abierto.
    base = _NativeBase([_answer(_row("motor_no_arranca", "flujo_pausado"))])
    _PlannerAnswers(base, _postgrest_error(ROWS["legacy_over_native"]))
    with pytest.raises(ResolutionError, match="^pilot_scope_rejected:flujo_nativo_en_curso$"):
        _resolve_native(base)

    assert base.names().count(PLANNER) == 1
    assert base.status_patches == [{
        "processing_status": "failed", "processed_at": None,
        "processing_error": "pilot_scope_rejected:flujo_nativo_en_curso",
    }]
