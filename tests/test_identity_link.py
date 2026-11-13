"""El correlador que decide solo (identity-link-decider-v1, E-536).

Contrato: ``docs/contracts/identity-link-decider-v1.md``. Las 18 filas de
``tests/fixtures/identity_link/conflictos-2026-11.json`` son los conflictos de
carrito medidos el 13/11 en Maribel y ATT1, como rasgos sin datos personales
(E-535). La base (los frenos, la evidencia y la re-verificacion) la prueba
``tests/sql/followup_engine/validate_identity_link_decider.mjs``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.identity_link import (
    CONFIRM_BUTTON,
    MAX_MODEL_ATTEMPTS,
    OPT_OUT_BUTTON,
    PROMPT_VERSION,
    REASON_CODES,
    IdentityLinkClaim,
    IdentityLinkClient,
    IdentityLinkEvidence,
    IdentityLinkNotice,
    IdentityLinkNotifier,
    IdentityLinkProviderError,
    IdentityLinkVerdict,
    IdentityLinkWorker,
    IdentityQuestionClaim,
    IdentityQuestionRound,
    IdentityQuestionTemplate,
    answer_for_reply,
    apply_rules,
    notice_command,
    record_reply_if_answer,
    verdict_from_model,
)
from bridge.messaging import FirstTouchResult
from bridge.opt_out import detect_explicit_opt_out
from bridge.template_request import OPT_OUT_BUTTON as PRODUCT_OPT_OUT_BUTTON

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/identity_link/conflictos-2026-11.json").read_text(encoding="utf-8"))
# El nombre de la plantilla en las pruebas es un placeholder: el real lo define
# Dan por instancia (sugerido <aliada>_compra_iniciada_01). El cuerpo definitivo
# de Dan del 13/11, para la prueba de los parametros: {{1}} el nombre del
# formulario, {{2}} el producto y {{3}} la marca (instancia.marca).
PLACEHOLDER_TEMPLATE = "aliada_compra_iniciada_01"
APPROVED_BODY = (
    "Hola {{1}}, te escribe el equipo de {{3}}. Vimos que empezaste tu compra de {{2}}. "
    "Si fuiste tú y quieres ayuda para terminarla, aquí estamos."
)


def _traits(row: dict[str, Any]) -> dict[str, object]:
    """La evidencia de la base para una fila; lo no medido, con un valor neutro."""
    form = row.get("pais_formulario", row.get("pais"))
    checkout = row.get("pais_checkout", row.get("pais"))
    ten = row.get("telefono_10_igual", row["telefono_canonico_igual"])
    return {
        "coincide_por": row["coincide_por"],
        "telefono_canonico_igual": row["telefono_canonico_igual"],
        "telefono_10_igual": ten,
        "telefono_7_igual": row.get("telefono_7_igual", ten),
        "pais_formulario": form,
        "pais_checkout": checkout,
        "mail_igual": row["mail_igual"],
        "dominio_igual": row.get("dominio_igual", row["mail_igual"]),
        "usuario_igual": row.get("usuario_igual", row["mail_igual"]),
        "parecido_usuario": row.get("parecido_usuario", 100 if row["mail_igual"] else 0),
        "parecido_dominio": 100 if row.get("dominio_igual", row["mail_igual"]) else 0,
        "parecido_nombre": row["parecido_nombre"],
        "primer_nombre_igual": row.get("primer_nombre_igual", row["parecido_nombre"] == 100),
        "minutos_formulario_a_carrito": 45,
        "misma_oferta": True,
        "otras_intenciones_7d": row.get("otras_intenciones_7d", 0),
        "conversacion_previa_con_el_telefono_del_permiso": False,
        "selector_pais_sospechoso": form == "NANP" and checkout == "MX" and ten,
    }


def _evidence(row: dict[str, Any]) -> IdentityLinkEvidence:
    # El freno sin_permiso lo pone la base (la lectura de evidencia).
    return IdentityLinkEvidence.from_snapshot({
        "version": "identity-link-evidence-v1",
        "persona": "Persona 1",
        "guard_reason": None if row["permiso"] else "sin_permiso",
        "rasgos": _traits(row),
    })


ROWS = [(f"{aliada}-{row['n']}", row) for aliada in ("maribel", "att1") for row in FIXTURE[aliada]]


@pytest.mark.parametrize(("label", "row"), ROWS, ids=[label for label, _ in ROWS])
def test_the_18_measured_conflicts_get_their_branch_without_the_model(label: str, row: dict[str, Any]) -> None:
    expected = row["esperado"]
    verdict = apply_rules(_evidence(row))
    assert verdict is not None, label
    assert verdict.branch == expected["rama"]
    if expected["rama"] == "ata":
        assert verdict.decided_by == expected["decidido_por"]
        assert verdict.destination_source == "intent_phone"
    else:
        assert verdict.guard_reason == expected["motivo"]
    with_d3 = apply_rules(_evidence(row), country_code_fix_enabled=True)
    assert with_d3 is not None
    if "con_d3" in expected:
        assert with_d3.branch == "ata"
        assert with_d3.decided_by == expected["con_d3"]["decidido_por"]
        assert with_d3.destination_source == expected["con_d3"]["destino"]
    else:
        assert with_d3 == verdict


def test_the_measured_totals_match_the_design() -> None:
    branches = [apply_rules(_evidence(row)).branch for _, row in ROWS]  # type: ignore[union-attr]
    assert branches.count("ata") == 14 and branches.count("nada") == 4 and "pregunta" not in branches
    with_d3 = [apply_rules(_evidence(row), country_code_fix_enabled=True).branch for _, row in ROWS]  # type: ignore[union-attr]
    assert with_d3.count("ata") == 16


def test_with_rules_first_off_the_clear_cases_go_to_the_model() -> None:
    row = FIXTURE["maribel"][0]
    assert apply_rules(_evidence(row), rules_first=False) is None
    # Los frenos siguen antes del modelo.
    assert apply_rules(_evidence(FIXTURE["maribel"][4]), rules_first=False).guard_reason == "sin_permiso"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "snapshot",
    [
        {"guard_reason": None, "rasgos": {**_traits(FIXTURE["maribel"][0]), "mail": "x@example.test"}},
        {"guard_reason": None, "rasgos": {**_traits(FIXTURE["maribel"][0]), "parecido_nombre": "Ana"}},
        {"guard_reason": None, "rasgos": {**_traits(FIXTURE["maribel"][0]), "pais_checkout": "5255"}},
        {"guard_reason": "otro_freno", "rasgos": _traits(FIXTURE["maribel"][0])},
        {"guard_reason": None},
        "no es un objeto",
    ],
)
def test_an_evidence_that_could_carry_personal_data_is_refused(snapshot: object) -> None:
    with pytest.raises(ValueError):
        IdentityLinkEvidence.from_snapshot(snapshot)


def test_the_model_context_has_only_traits() -> None:
    context = _evidence(FIXTURE["maribel"][0]).to_model_context()
    assert context["persona"] == "Persona 1"
    text = json.dumps(context)
    # Ni mails ni numeros con forma de telefono: solo si/no, 0-100 y conteos.
    assert "@" not in text
    assert all(len(token) < 4 for token in __import__("re").findall(r"\d+", text))


# ---------------------------------------------------------------------------
# El validador del modelo: toda salida mala termina en «nada».
# ---------------------------------------------------------------------------

UNSURE = _evidence({**FIXTURE["maribel"][1], "telefono_canonico_igual": False, "telefono_10_igual": False,
                    "mail_igual": False, "coincide_por": "mail"})


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        ({"decision": "same_person", "confidence": "high"}, "modelo_respuesta_invalida"),
        ({"decision": "same_person", "confidence": "high", "reason_codes": ["phone_canonical_equal"], "extra": 1},
         "modelo_respuesta_invalida"),
        ({"decision": "yes", "confidence": "high", "reason_codes": []}, "modelo_respuesta_invalida"),
        ({"decision": "same_person", "confidence": "high", "reason_codes": ["porque si"]}, "modelo_respuesta_invalida"),
        ({"decision": "same_person", "confidence": "high", "reason_codes": ["name_equal", "name_equal"]},
         "modelo_respuesta_invalida"),
        ({"_invalid": True}, "modelo_respuesta_invalida"),
        ({"decision": "same_person", "confidence": "high", "reason_codes": ["name_equal"]}, "modelo_sin_rasgo_fuerte"),
        # Cita un rasgo fuerte que en la evidencia es falso.
        ({"decision": "same_person", "confidence": "high", "reason_codes": ["phone_canonical_equal"]},
         "modelo_sin_rasgo_fuerte"),
        ({"decision": "same_person", "confidence": "low", "reason_codes": []}, "modelo_confianza_baja"),
        ({"decision": "different_person", "confidence": "high", "reason_codes": ["name_differs"]},
         "modelo_otra_persona"),
    ],
)
def test_a_bad_model_answer_never_ties(proposal: object, reason: str) -> None:
    verdict = verdict_from_model(proposal, UNSURE, model_name="modelo/prueba", ask_enabled=True)
    assert verdict.branch == "nada"
    assert verdict.guard_reason == reason
    assert verdict.decided_by == "modelo:modelo/prueba"


def test_a_strong_trait_that_is_true_ties() -> None:
    evidence = _evidence(FIXTURE["maribel"][5])  # el mail igual
    verdict = verdict_from_model(
        {"decision": "same_person", "confidence": "high", "reason_codes": ["email_exact_equal", "name_equal"]},
        evidence, model_name="m", ask_enabled=False,
    )
    assert verdict.branch == "ata" and verdict.destination_source == "intent_phone"
    assert verdict.model_decision == "same_person" and verdict.prompt_version == PROMPT_VERSION


@pytest.mark.parametrize("proposal", [
    {"decision": "unsure", "confidence": "high", "reason_codes": []},
    {"decision": "same_person", "confidence": "medium", "reason_codes": ["name_similar"]},
])
def test_doubt_asks_only_with_the_block_b(proposal: dict[str, object]) -> None:
    asked = verdict_from_model(proposal, UNSURE, model_name="m", ask_enabled=True)
    assert asked.branch == "pregunta" and asked.destination_source == "intent_phone"
    quiet = verdict_from_model(proposal, UNSURE, model_name="m", ask_enabled=False)
    assert quiet.branch == "nada" and quiet.guard_reason == "habria_preguntado"


def test_the_plus_one_never_is_a_destination_for_the_model_either() -> None:
    plus_one = _evidence(FIXTURE["maribel"][3])
    proposal = {"decision": "same_person", "confidence": "high", "reason_codes": ["email_exact_equal"]}
    assert verdict_from_model(proposal, plus_one, model_name="m", ask_enabled=True).guard_reason == "destino_sin_permiso"
    fixed = verdict_from_model(proposal, plus_one, model_name="m", ask_enabled=True, country_code_fix_enabled=True)
    assert fixed.branch == "ata" and fixed.destination_source == "intent_national_digits"


def test_the_reason_codes_are_a_closed_list_of_machine_tokens() -> None:
    assert {"phone_canonical_equal", "email_exact_equal", "phone_national_equal_country_differs"} <= REASON_CODES
    assert all(code.replace("_", "").isalnum() and code == code.lower() for code in REASON_CODES)


def test_the_client_sends_only_traits_and_keeps_no_text() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decision": "unsure", "confidence": "medium", "reason_codes": ["name_equal"]})}}]})

    client = IdentityLinkClient(base_url="http://hermes:8642/v1", api_key="k", model_name="modelo/prueba",
                                transport=httpx.MockTransport(handler))
    proposal = asyncio.run(client.propose(UNSURE, case_id="11111111-1111-4111-8111-111111111111"))
    assert proposal["decision"] == "unsure"
    user = json.loads(seen[0]["messages"][1]["content"])
    assert set(user) == {"persona", "rasgos"} and "@" not in json.dumps(user)


def test_a_provider_error_is_retryable() -> None:
    client = IdentityLinkClient(base_url="http://hermes:8642/v1", api_key="k", model_name="m",
                                transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(IdentityLinkProviderError):
        asyncio.run(client.propose(UNSURE, case_id="c"))


# ---------------------------------------------------------------------------
# El worker con una base y un modelo falsos.
# ---------------------------------------------------------------------------

@dataclass
class _Model:
    answer: object = None
    error: bool = False
    model_name: str = "modelo/prueba"
    prompt_version: str = PROMPT_VERSION
    calls: int = 0

    async def propose(self, evidence: IdentityLinkEvidence, *, case_id: str) -> dict[str, object]:
        self.calls += 1
        if self.error:
            raise IdentityLinkProviderError("identity_link_provider_http_error")
        return self.answer  # type: ignore[return-value]


class _Store:
    def __init__(self, evidence: dict[str, object], *, attempt: int = 1) -> None:
        self.claim = IdentityLinkClaim(
            webhook_event_id="11111111-1111-4111-8111-111111111111",
            claim_token="22222222-2222-4222-8222-222222222222",
            lease_generation=1, attempt_count=attempt, evidence=evidence, evidence_sha256="a" * 64,
        )
        self.completed: list[tuple[str, IdentityLinkVerdict]] = []
        self.released: list[str] = []
        self.requeues = 0

    async def claim_identity_link_case(self, **_: object) -> IdentityLinkClaim | None:
        claim, self.claim = self.claim, None  # type: ignore[assignment]
        return claim

    async def complete_identity_link_decision(self, *, claim: IdentityLinkClaim, mode: str,
                                              verdict: IdentityLinkVerdict) -> dict[str, object]:
        self.completed.append((mode, verdict))
        return {"branch": verdict.branch, "effect": "sombra" if mode == "sombra" else "requeued"}

    async def release_identity_link_case(self, *, claim: IdentityLinkClaim, failure_code: str) -> None:
        self.released.append(failure_code)

    async def requeue_identity_linked_events(self, **_: object) -> int:
        self.requeues += 1
        return 0


def _snapshot(row: dict[str, Any], guard: str | None = None) -> dict[str, object]:
    return {"version": "identity-link-evidence-v1", "persona": "Persona 1", "guard_reason": guard, "rasgos": _traits(row)}


def _worker(store: _Store, model: _Model, **kwargs: Any) -> IdentityLinkWorker:
    return IdentityLinkWorker(store=store, model=model, tenant_ref="maribel", funnel_ref="elprotocolo",
                              worker_id="w", **{"mode": "vivo", **kwargs})


def test_the_rule_ties_without_calling_the_model() -> None:
    store, model = _Store(_snapshot(FIXTURE["maribel"][0])), _Model()
    assert asyncio.run(_worker(store, model).run_once()) == 1
    assert model.calls == 0 and store.requeues == 1
    [(mode, verdict)] = store.completed
    assert mode == "vivo" and verdict.branch == "ata" and verdict.decided_by == "regla:R1"


def test_shadow_decides_the_same_and_says_shadow() -> None:
    store = _Store(_snapshot(FIXTURE["maribel"][5]))
    asyncio.run(_worker(store, _Model(), mode="sombra").run_once())
    [(mode, verdict)] = store.completed
    assert mode == "sombra" and verdict.decided_by == "regla:R2"


def test_the_model_unsure_asks_only_with_b() -> None:
    row = {**FIXTURE["maribel"][1], "telefono_canonico_igual": False, "telefono_10_igual": False, "coincide_por": "mail"}
    answer = {"decision": "unsure", "confidence": "medium", "reason_codes": []}
    store = _Store(_snapshot(row))
    asyncio.run(_worker(store, _Model(answer=answer)).run_once())
    assert store.completed[0][1].guard_reason == "habria_preguntado"
    store = _Store(_snapshot(row))
    asyncio.run(_worker(store, _Model(answer=answer), ask_enabled=True).run_once())
    assert store.completed[0][1].branch == "pregunta"


def test_a_provider_error_releases_and_on_the_third_attempt_is_nothing() -> None:
    row = {**FIXTURE["maribel"][1], "telefono_canonico_igual": False, "telefono_10_igual": False, "coincide_por": "mail"}
    store = _Store(_snapshot(row), attempt=1)
    assert asyncio.run(_worker(store, _Model(error=True)).run_once()) == 0
    assert store.released == ["identity_link_model_retry"] and not store.completed
    store = _Store(_snapshot(row), attempt=MAX_MODEL_ATTEMPTS)
    asyncio.run(_worker(store, _Model(error=True)).run_once())
    [(_, verdict)] = store.completed
    assert verdict.branch == "nada" and verdict.guard_reason == "modelo_no_disponible"


def test_a_guard_from_the_base_wins_and_an_invalid_evidence_is_nothing() -> None:
    store = _Store(_snapshot(FIXTURE["maribel"][0], guard="ya_compro"))
    asyncio.run(_worker(store, _Model()).run_once())
    assert store.completed[0][1].guard_reason == "ya_compro" and store.completed[0][1].decided_by == "freno"
    store = _Store({"guard_reason": None, "rasgos": {"mail": "x@example.test"}})
    asyncio.run(_worker(store, _Model()).run_once())
    assert store.completed[0][1].guard_reason == "evidencia_invalida"


def test_a_decision_the_base_refuses_becomes_nothing_and_does_not_loop() -> None:
    class Refusing(_Store):
        async def complete_identity_link_decision(self, *, claim: IdentityLinkClaim, mode: str,
                                                  verdict: IdentityLinkVerdict) -> dict[str, object]:
            if verdict.branch == "ata":
                raise RuntimeError("22023 identity_link_rule_not_met")
            return await super().complete_identity_link_decision(claim=claim, mode=mode, verdict=verdict)

    store = Refusing(_snapshot(FIXTURE["maribel"][0]))
    assert asyncio.run(_worker(store, _Model()).run_once()) == 1
    [(_, verdict)] = store.completed
    assert verdict.branch == "nada" and verdict.guard_reason == "decision_rechazada"


@pytest.mark.parametrize(("kwargs", "error"), [
    ({"mode": "off"}, "invalid_identity_link_mode"),
    ({"mode": "sombra", "ask_enabled": True}, "identity_link_ask_requires_vivo"),
    ({"poll_interval_seconds": 0}, "invalid_identity_link_poll_interval"),
])
def test_the_worker_refuses_an_incomplete_configuration(kwargs: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _worker(_Store({}), _Model(), **kwargs)


# ---------------------------------------------------------------------------
# Los avisos al canal: sin telefonos ni mails, con la rama y la conversacion.
# ---------------------------------------------------------------------------

def test_a_notice_carries_the_branch_and_the_conversation_and_nothing_personal() -> None:
    command = notice_command(IdentityLinkNotice(
        notice_key="decision:33333333-3333-4333-8333-333333333333", kind="decision", branch="ata",
        decided_by="regla:R1", outcome=None, conversation_id=1234, occurred_at=datetime(2026, 11, 13, tzinfo=UTC),
    ))
    assert command.event_code == "IDL-001"
    assert command.reason_code == "ata:regla:r1"
    assert command.component == "chatwoot.conversation.1234"
    send = notice_command(IdentityLinkNotice(
        notice_key="envio:44444444-4444-4444-8444-444444444444:falla", kind="envio", branch="ata",
        decided_by="modelo:z-ai/glm-5.3", outcome="falla", conversation_id=None,
        occurred_at=datetime(2026, 11, 13, tzinfo=UTC),
    ))
    assert send.event_code == "IDL-002" and send.state == "falla"
    assert send.component == "identity_link.sin_conversacion"


def test_the_notifier_marks_what_slack_took_and_retries_what_it_did_not() -> None:
    notices = [IdentityLinkNotice(notice_key=f"decision:{i}{'3' * 7}-3333-4333-8333-333333333333", kind="decision",
                                  branch="ata", decided_by="regla:R1", outcome=None, conversation_id=7,
                                  occurred_at=datetime(2026, 11, 13, tzinfo=UTC)) for i in range(2)]
    marked: list[str] = []

    class Store:
        async def read_identity_link_notices(self, **_: object) -> list[IdentityLinkNotice]:
            return notices

        async def mark_identity_link_notice(self, *, notice_key: str, **_: object) -> bool:
            marked.append(notice_key)
            return True

    class Producer:
        def __init__(self) -> None:
            self.calls = 0

        async def admit(self, command: object) -> object:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("slack caido")
            return command

    notifier = IdentityLinkNotifier(store=Store(), producer=Producer(), tenant_ref="t", funnel_ref="f")
    assert asyncio.run(notifier.run_once()) == 1
    assert marked == [notices[0].notice_key]
    assert IdentityLinkNotifier(store=Store(), producer=None, tenant_ref="t", funnel_ref="f").alerts == "sin_slack"


# ---------------------------------------------------------------------------
# Bloque B: la pregunta y la respuesta.
# ---------------------------------------------------------------------------

def test_the_opt_out_button_is_the_product_opt_out() -> None:
    assert OPT_OUT_BUTTON == PRODUCT_OPT_OUT_BUTTON
    assert detect_explicit_opt_out([OPT_OUT_BUTTON]) is not None
    assert detect_explicit_opt_out([CONFIRM_BUTTON]) is None


@pytest.mark.parametrize(("content", "answer"), [
    ("Sí, fui yo", "si"), ("si, fui yo", "si"), ("  Sí,  fui yo ", "si"),
    ("No más mensajes", "baja"), ("Sí", None), ("fui yo pero no compro", None), (None, None),
])
def test_the_buttons_are_recognized_and_nothing_else(content: object, answer: str | None) -> None:
    assert answer_for_reply(content) == answer


def test_the_template_is_configurable_and_carries_name_and_product() -> None:
    template = IdentityQuestionTemplate(name=PLACEHOLDER_TEMPLATE, language="es_MX")
    params = template.params(greeting_name="Ana", product_name="El Protocolo", brand="Maribel")
    assert params == {"name": PLACEHOLDER_TEMPLATE, "category": "MARKETING", "language": "es_MX",
                      "processed_params": {"body": {"1": "Ana", "2": "El Protocolo", "3": "Maribel"}}}
    # El default sigue la numeracion del texto aprobado: {{1}} nombre, {{2}} producto, {{3}} marca.
    assert sorted(set(re.findall(r"\{\{(\d)\}\}", APPROVED_BODY))) == ["1", "2", "3"]
    assert template.parameters == ("nombre", "producto", "marca")
    with pytest.raises(ValueError):
        IdentityQuestionTemplate(name=PLACEHOLDER_TEMPLATE, language="es_MX", parameters=("nombre", "nombre"))
    with pytest.raises(ValueError):
        IdentityQuestionTemplate(name="Con Espacios", language="es_MX")
    with pytest.raises(ValueError):
        IdentityQuestionTemplate(name=PLACEHOLDER_TEMPLATE, language="es_MX", parameters=("nombre", "vence"))


class _QuestionStore:
    def __init__(self, claim: IdentityQuestionClaim | None, start: str = "ok") -> None:
        self.claim = claim
        self.start = start
        self.finished: list[dict[str, object]] = []
        self.claimed = 0

    async def expire_identity_link_questions(self, **_: object) -> int:
        return 0

    async def claim_identity_link_question(self, **_: object) -> IdentityQuestionClaim | None:
        self.claimed += 1
        return self.claim

    async def start_identity_link_question(self, *, claim: IdentityQuestionClaim) -> str:
        return self.start

    async def finish_identity_link_question(self, **kwargs: object) -> None:
        self.finished.append(kwargs)


class _Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_first_touch(self, **kwargs: object) -> FirstTouchResult:
        self.calls.append(kwargs)
        return FirstTouchResult(status="sent", conversation_id=4321, message_id=99)


QUESTION = IdentityQuestionClaim(decision_id="55555555-5555-4555-8555-555555555555",
                                 claim_token="66666666-6666-4666-8666-666666666666", lease_generation=1,
                                 phone="525512345678", full_name="Ana María López", product_name="El Protocolo")


def _round(store: _QuestionStore, sender: _Sender, hour: int) -> IdentityQuestionRound:
    return IdentityQuestionRound(
        store=store, sender=sender, template=IdentityQuestionTemplate(name=PLACEHOLDER_TEMPLATE, language="es_MX"),
        tenant_ref="maribel", funnel_ref="elprotocolo", worker_id="w", brand="El Protocolo", time_zone="America/Mexico_City",
        window=(time(9), time(21)), clock=lambda: datetime(2026, 11, 13, tzinfo=UTC) + timedelta(hours=hour + 6),
    )


def test_the_question_goes_to_the_consented_phone_with_the_template() -> None:
    store, sender = _QuestionStore(QUESTION), _Sender()
    assert asyncio.run(_round(store, sender, hour=10)()) == 1
    [call] = sender.calls
    assert call["phone"] == "525512345678"
    assert call["template_params"]["name"] == PLACEHOLDER_TEMPLATE  # type: ignore[index]
    assert call["template_params"]["processed_params"]["body"] == {  # type: ignore[index]
        "1": "Ana", "2": "El Protocolo", "3": "El Protocolo"}
    assert store.finished[0]["outcome"] == "sent" and store.finished[0]["conversation_id"] == 4321


def test_out_of_the_window_nothing_is_claimed() -> None:
    store, sender = _QuestionStore(QUESTION), _Sender()
    assert asyncio.run(_round(store, sender, hour=22)()) == 0
    assert store.claimed == 0 and not sender.calls


def test_the_gate_of_the_base_stops_the_send() -> None:
    store, sender = _QuestionStore(QUESTION, start="identity_link_ya_compro"), _Sender()
    asyncio.run(_round(store, sender, hour=10)())
    assert not sender.calls and not store.finished


def test_an_unsafe_name_closes_without_sending() -> None:
    claim = IdentityQuestionClaim(**{**QUESTION.__dict__, "full_name": "http://spam.example"})
    store, sender = _QuestionStore(claim), _Sender()
    asyncio.run(_round(store, sender, hour=10)())
    assert not sender.calls and store.finished[0]["failure_code"] == "template_parameters_missing"


def test_without_the_brand_it_closes_without_sending() -> None:
    store, sender = _QuestionStore(QUESTION), _Sender()
    question = _round(store, sender, hour=10)
    question.brand = "  "
    asyncio.run(question())
    assert not sender.calls and store.finished[0]["failure_code"] == "template_parameters_missing"


def test_a_crash_of_the_sender_is_unknown_and_never_retried() -> None:
    class Broken(_Sender):
        async def send_first_touch(self, **kwargs: object) -> FirstTouchResult:
            raise httpx.ReadTimeout("t")

    store = _QuestionStore(QUESTION)
    asyncio.run(_round(store, Broken(), hour=10)())
    assert store.finished[0]["outcome"] == "unknown"


def test_the_reply_is_recorded_softly() -> None:
    recorded: list[dict[str, object]] = []

    class Store:
        async def record_identity_link_answer(self, **kwargs: object) -> dict[str, object]:
            recorded.append(kwargs)
            return {"recorded": True}

    answer = asyncio.run(record_reply_if_answer(Store(), tenant_ref="t", funnel_ref="f", conversation_id=4321,
                                                content="Sí, fui yo", message_id=7))
    assert answer == "si" and recorded[0]["answer"] == "si"
    assert asyncio.run(record_reply_if_answer(Store(), tenant_ref="t", funnel_ref="f", conversation_id=4321,
                                              content="hola", message_id=8)) is None

    class Broken:
        async def record_identity_link_answer(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("base caida")

    assert asyncio.run(record_reply_if_answer(Broken(), tenant_ref="t", funnel_ref="f", conversation_id=1,
                                              content="No más mensajes", message_id=None)) is None
