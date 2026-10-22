"""El despachador manda un paso del motor con la plantilla de su version (la T2.4, commit 5).

Contrato: ``docs/contracts/flow-engine-v1.md``, secciones 7.2, 7.3 y 8. Plan de
la T2.4, commit 5, y decisiones D3 y D5.

Reusa el arnes de A1, A2 y Z (``test_cart_followups``, sobre
``test_cart_discount_first_touch``): la base falsa, el Chatwoot falso y el
catalogo capturado del inbox 11 del 18/10. Ese arnes reemplaza la verificacion
de Chatwoot por una que siempre aprueba; la verificacion real la corre
``test_followup_chatwoot_identity`` y este cambio no la toca. El contexto del
paso y las filas del arranque salen de ``tests/fixtures/flow_engine/
bridge_rpc_rows.json`` (la base real, PGlite).

Lo que factura (a): con el flag prendido, una accion del legado no hace ninguna
llamada nueva, arranca por ``mark_followup_request_started`` y manda el mismo
mensaje que con el flag apagado.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import CARRITO_CATALOG, _CartAuthority
from test_cart_followups import EVIDENCE, FOLLOWUPS, _StepAuthority
from test_cart_followups import _dispatcher as _followup_dispatcher
from test_durable_dispatcher_approved_template import CATALOG, NOW
from test_payment_slip_reminders import _ReminderChatwoot

from bridge.cart_followups import CART_LAST_TOUCH_STEP, CART_SECOND_TOUCH_STEP
from bridge.messaging import FinalMetaEffect
from bridge.supabase import (
    FlowEngineRunTerminated,
    FlowEngineStepContext,
    PilotRequestStartRejectedError,
    SupabaseError,
)
from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED, DurableDispatcher

ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
FLOW_POLICY = "flujo:00000000-0000-4000-8000-000000000011"
OTHER_A2 = "att1_carrito_abandonado_01"


def _context(key: str, **changes: Any) -> FlowEngineStepContext:
    row = ROWS["context"][key]
    context = FlowEngineStepContext(
        flow_id=row["flow_id"],
        version=row["version"],
        product_ref=row["product_ref"],
        flow_key=row["flow_key"],
        step_key=row["step_key"],
        send_ordinal=row["send_ordinal"],
        template_name=row["template_name"],
        template_language=row["template_language"],
        template_variables=tuple(row["template_variables"]),
        template_button=row["template_button"],
        legacy_dispatch_ok=row["legacy_dispatch_ok"],
    )
    return replace(context, **changes)


class _Engine:
    """Lo que se le agrega a la base falsa: el contexto y el arranque del motor."""

    def _engine_setup(
        self,
        *,
        engine: bool,
        context: FlowEngineStepContext | None,
        context_error: bool,
        start: str,
    ) -> None:
        if engine:
            self.action = replace(self.action, policy_key=FLOW_POLICY)  # type: ignore[attr-defined]
        self.engine_context = context
        self.engine_context_error = context_error
        self.engine_start = start
        self.engine_calls: list[str] = []

    async def get_flow_engine_step_context(self, action_id: str) -> FlowEngineStepContext | None:
        self.engine_calls.append("context")
        if self.engine_context_error:
            raise SupabaseError("flow_engine_execution_context_v1_failed: HTTP 503")
        return self.engine_context

    async def mark_flow_engine_request_started(self, **_: object) -> Any:
        self.engine_calls.append("request_started")
        self.events.append("engine_request_started")  # type: ignore[attr-defined]
        if self.engine_start == "rechazado":
            raise PilotRequestStartRejectedError(ROWS["request_start"]["rechazado"]["reason"])
        if self.engine_start == "terminado":
            raise FlowEngineRunTerminated(ROWS["request_start"]["terminado"]["reason"])
        return replace(self.attempt, phase="request_started")  # type: ignore[attr-defined]


class _EngineStep(_Engine, _StepAuthority):
    def __init__(self, step_key: str, *, engine: bool = True, context: FlowEngineStepContext | None = None,
                 context_error: bool = False, start: str = "request_started", **kwargs: Any) -> None:
        super().__init__(step_key, **kwargs)
        self._engine_setup(engine=engine, context=context, context_error=context_error, start=start)


class _EngineFirst(_Engine, _CartAuthority):
    def __init__(self, *, engine: bool = True, context: FlowEngineStepContext | None = None,
                 context_error: bool = False, start: str = "request_started", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._engine_setup(engine=engine, context=context, context_error=context_error, start=start)


class _SpyGate:
    def __init__(self) -> None:
        self.effects: list[FinalMetaEffect] = []

    def authorize(self, effect: FinalMetaEffect) -> bool:
        self.effects.append(effect)
        return True


def _run(authority: Any, tmp_path: Path, *, flag: bool = True) -> tuple[Any, _SpyGate]:
    chatwoot = _ReminderChatwoot([*CATALOG, *CARRITO_CATALOG])
    dispatcher: DurableDispatcher = _followup_dispatcher(authority, chatwoot, tmp_path, followups=FOLLOWUPS)
    dispatcher._flow_engine_enabled = flag  # type: ignore[attr-defined]
    gate = _SpyGate()
    dispatcher._final_meta_effect_gate = gate  # type: ignore[assignment]
    asyncio.run(dispatcher.dispatch_due(now=NOW))
    return chatwoot, gate


def _sent(chatwoot: Any) -> list[dict[str, Any]]:
    """Lo que salio con plantilla: A1 abre la conversacion, A2 y Z van a la 200."""
    posts = [*chatwoot.posts("/conversations/200/messages"), *chatwoot.posts("/conversations")]
    return [post for post in posts if "template_params" in post or "message" in post]


# ------------------------------------------------------------- lo que factura


@pytest.mark.parametrize("step_key", ["first_contact", CART_SECOND_TOUCH_STEP])
def test_a_a_legacy_action_with_the_flag_on_is_exactly_today(tmp_path: Path, step_key: str) -> None:
    def authority(engine: bool) -> Any:
        if step_key == "first_contact":
            return _EngineFirst(engine=engine, context=_context(step_key))
        return _EngineStep(step_key, engine=engine, context=_context(step_key))

    off = authority(False)
    off_chatwoot, off_gate = _run(off, tmp_path / "off", flag=False)
    on = authority(False)
    on_chatwoot, on_gate = _run(on, tmp_path / "on", flag=True)

    assert on.engine_calls == []
    assert on.events == off.events
    assert "request_started" in on.events and "engine_request_started" not in on.events
    assert _sent(on_chatwoot) == _sent(off_chatwoot) != []
    assert [effect.template_language for effect in on_gate.effects] == [
        effect.template_language for effect in off_gate.effects
    ]


def test_f_the_flag_off_ignores_an_engine_action(tmp_path: Path) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP,
                                                                       template_name=OTHER_A2))
    chatwoot, _ = _run(authority, tmp_path, flag=False)

    assert authority.engine_calls == []
    assert "request_started" in authority.events
    [message] = _sent(chatwoot)
    assert message["template_params"]["name"] == FOLLOWUPS.second_touch_template


# ---------------------------------------------------------------- con el motor


def test_b_an_engine_a2_goes_out_with_the_template_of_its_version(tmp_path: Path) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP,
                                                                       template_name=OTHER_A2))
    chatwoot, gate = _run(authority, tmp_path)

    assert authority.engine_calls == ["context", "request_started"]
    assert "request_started" not in authority.events
    [message] = _sent(chatwoot)
    assert message["template_params"]["name"] == OTHER_A2
    assert message["template_params"]["language"] == "es_MX"
    assert authority.acceptances


def test_b_the_final_gate_hashes_the_language_of_the_version(tmp_path: Path) -> None:
    # La configuracion del arnes es es_EC; la version fijada dice es_MX.
    authority = _EngineFirst(context=_context("first_contact"))
    _, gate = _run(authority, tmp_path)
    assert [effect.template_language for effect in gate.effects] == ["es_MX"]
    legacy = _EngineFirst(engine=False, context=_context("first_contact"))
    _, legacy_gate = _run(legacy, tmp_path / "legado")
    assert [effect.template_language for effect in legacy_gate.effects] == ["es_EC"]


@pytest.mark.parametrize("step_key", ["first_contact", CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP])
def test_b_every_cart_step_of_the_engine_starts_by_the_engine(tmp_path: Path, step_key: str) -> None:
    if step_key == "first_contact":
        authority: Any = _EngineFirst(context=_context(step_key))
    else:
        authority = _EngineStep(step_key, context=_context(step_key))
    chatwoot, _ = _run(authority, tmp_path)

    assert authority.engine_calls == ["context", "request_started"]
    [message] = _sent(chatwoot)
    assert message["template_params"]["name"] == ROWS["context"][step_key]["template_name"]


def test_c_a_template_of_the_version_outside_the_catalog_does_not_go_out(tmp_path: Path) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP,
                                                                       template_name="att1_no_existe_01"))
    chatwoot, _ = _run(authority, tmp_path)

    assert _sent(chatwoot) == []
    assert authority.engine_calls == ["context"]
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"


def test_d_rechazado_leaves_the_attempt_reserved_and_the_batch_goes_on(tmp_path: Path) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP), start="rechazado")
    chatwoot, _ = _run(authority, tmp_path)

    assert authority.engine_calls == ["context", "request_started"]
    assert _sent(chatwoot) == []
    assert authority.finalizations == []
    assert authority.acceptances == []


def test_e_terminado_neither_retries_nor_closes_twice(tmp_path: Path) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP), start="terminado")
    chatwoot, _ = _run(authority, tmp_path)

    assert authority.engine_calls == ["context", "request_started"]
    assert _sent(chatwoot) == []
    assert authority.finalizations == []
    assert authority.acceptances == []


@pytest.mark.parametrize(
    "changes",
    [
        {"template_variables": ("nombre",)},
        {"template_variables": ("producto", "nombre")},
        {"template_button": {"link": "checkout", "cupon": "personal"}},
        {"step_key": CART_LAST_TOUCH_STEP},
    ],
)
def test_g_a_version_the_bridge_cannot_build_closes_without_retry(tmp_path: Path, changes: dict[str, Any]) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=_context(CART_SECOND_TOUCH_STEP, **changes))
    chatwoot, _ = _run(authority, tmp_path)

    assert _sent(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED
    assert closed["next_attempt_at"] is None
    assert "request_started" not in authority.engine_calls


def test_g_a_first_touch_without_its_button_is_unsupported(tmp_path: Path) -> None:
    authority = _EngineFirst(context=_context("first_contact", template_button=None))
    chatwoot, _ = _run(authority, tmp_path)
    assert _sent(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED


@pytest.mark.parametrize("context_error", [True, False])
def test_h_without_a_context_it_goes_the_way_of_today(tmp_path: Path, context_error: bool) -> None:
    authority = _EngineStep(CART_SECOND_TOUCH_STEP, context=None, context_error=context_error)
    chatwoot, _ = _run(authority, tmp_path)

    assert authority.engine_calls == ["context"]
    assert "request_started" in authority.events
    [message] = _sent(chatwoot)
    assert message["template_params"]["name"] == FOLLOWUPS.second_touch_template


def test_the_evidence_of_the_harness_is_the_one_of_the_followups() -> None:
    # El arnes es el de A2/Z: si cambia, este archivo tiene que mirarlo.
    assert EVIDENCE["p_chatwoot_conversation_id"] == "200"


@pytest.mark.parametrize("step_key", [CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP])
@pytest.mark.parametrize("changes", [{"template_name": "att1_no_existe_01"}, {"template_language": "en_US"}])
def test_b_a2_and_z_look_up_the_template_and_language_of_the_version(
    tmp_path: Path, step_key: str, changes: dict[str, Any]
) -> None:
    # El catalogo capturado no tiene esa plantilla ni ese idioma: si el paso
    # usara la configuracion, saldria. Que no salga nada prueba el reemplazo.
    authority = _EngineStep(step_key, context=_context(step_key, **changes))
    chatwoot, _ = _run(authority, tmp_path)

    assert _sent(chatwoot) == []
    assert authority.engine_calls == ["context"]


@pytest.mark.parametrize("changes", [{"template_name": "att1_no_existe_01"}, {"template_language": "en_US"}])
def test_b_a1_without_the_template_of_its_version_falls_back_like_today(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    # A1 no se pierde por el cupon ni por su plantilla (a1_espera_al_cupon):
    # sin la de la version sale la del manifiesto, sin descuento, como hoy. El
    # porton de Meta lleva el idioma de lo que sale, no el de la version.
    authority = _EngineFirst(context=_context("first_contact", **changes))
    chatwoot, gate = _run(authority, tmp_path)

    [message] = _sent(chatwoot)
    assert message.get("template_params", {}).get("name") != ROWS["context"]["first_contact"]["template_name"]
    assert authority.engine_calls == ["context", "request_started"]
    assert [effect.template_language for effect in gate.effects] == ["es_EC"]
