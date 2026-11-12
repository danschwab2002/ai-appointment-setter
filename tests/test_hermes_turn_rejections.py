"""E-486 (v1.37.0, item 4): los turnos fallidos con mensajes simples.

Las cuatro formas reales leidas el 12/11 en el state.db de Hermes (solo la
forma; el texto de aca es sintetico, del mismo largo):

- Maribel #120 y ATT1 #726: JSON valido con la clave `product` en una
  instancia sin [[productos]] -> se descartaba el turno entero.
- ATT1 #728: `reply` de 1086 y 1035 caracteres -> el reintento repetia el
  mismo pedido sin decir que estuvo mal.
- Maribel #39: prosa sin JSON las dos veces -> sigue derivando, con log.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from bridge.event_log import LOGGED_INFO_EVENTS
from bridge.hermes import (
    HermesShadowProcessor,
    normalize_proposal,
    proposal_rejection_reason,
    retry_correction,
)


def _proposal(**overrides: object) -> dict[str, object]:
    proposal: dict[str, object] = {
        "decision": "send_payment_link",
        "qualification_status": "in_progress",
        "reason_code": "payment_link_requested",
        "reply": "Claro, con gusto.",
        "captured_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "missing_fields": [],
    }
    proposal.update(overrides)
    return proposal


def _processor(
    tmp_path: Path, contents: list[str], **kwargs: object
) -> tuple[HermesShadowProcessor, list[dict[str, object]]]:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = contents[min(len(requests), len(contents)) - 1]
        return httpx.Response(200, json={"model": "agente-comercial", "choices": [{"message": {"content": content}}]})

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )
    return processor, requests


def _run(processor: HermesShadowProcessor, delivery_id: str, ref: str = "120") -> None:
    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": ref, "messages": [{"role": "user", "content": "Retomar proceso"}]},
        )
    )


# ------------------------------------------------------------- reglas


@pytest.mark.parametrize(
    ("proposal", "rule"),
    [
        ("prosa", "not_json"),
        (_proposal(extra=1), "extra_key"),
        (_proposal(product="att1"), "extra_key"),
        (_proposal(reply="x" * 1086), "reply_too_long"),
        (_proposal(reply=""), "reply_empty"),
        (_proposal(reply="¿Uno? ¿Dos?"), "too_many_questions"),
        (_proposal(reply="https://pay.example.test"), "url_in_reply"),
        (_proposal(decision="otra"), "decision"),
        (_proposal(qualification_status="qualified"), "qualification_status"),
        (_proposal(reason_code="Con Espacios"), "reason_code"),
        (_proposal(missing_fields="x"), "schema"),
    ],
)
def test_each_rejection_names_its_rule(proposal: object, rule: str) -> None:
    assert proposal_rejection_reason(proposal) == rule


def test_a_valid_proposal_has_no_rejection() -> None:
    assert proposal_rejection_reason(_proposal()) is None


def test_product_is_dropped_only_without_declared_products() -> None:
    with_product = _proposal(product="maribel")
    assert "product" not in normalize_proposal(with_product, frozenset())
    # Con productos declarados no se ablanda: uno que no existe sigue invalido.
    kept = normalize_proposal(_proposal(product="otro"), frozenset({"att1"}))
    assert kept["product"] == "otro"
    assert proposal_rejection_reason(kept, frozenset({"att1"})) == "unknown_product"


def test_the_correction_says_the_rule_and_never_the_text() -> None:
    long_reply = _proposal(reply="secreto " * 136)
    message = retry_correction("reply_too_long", long_reply)
    assert "1088 caracteres" in message and "1000" in message
    assert "secreto" not in message
    for rule in ("not_json", "extra_key", "too_many_questions", "schema", "rara"):
        assert retry_correction(rule).startswith("Tu respuesta anterior no se pudo usar:")


# ----------------------------------------------------- el turno entero


def test_product_without_products_completes_on_the_first_attempt(tmp_path: Path) -> None:
    """Maribel #120: «Retomar proceso» terminaba derivado por la clave `product`."""
    processor, requests = _processor(
        tmp_path, [json.dumps(_proposal(product="el-protocolo"), ensure_ascii=False)]
    )

    _run(processor, "maribel-120")

    proposal = processor.get_completed_proposal(delivery_id="maribel-120")
    assert proposal is not None
    assert proposal["decision"] == "send_payment_link"
    assert "product" not in proposal
    assert len(requests) == 1


def test_a_long_reply_retries_with_the_correction(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """ATT1 #728: el segundo pedido le dice al modelo que acorte el reply."""
    processor, requests = _processor(
        tmp_path,
        [
            json.dumps(_proposal(reply="a" * 1086)),
            json.dumps(_proposal(reply="Corto.")),
        ],
    )

    with caplog.at_level(logging.INFO, logger="bridge.hermes"):
        _run(processor, "att1-728", ref="728")

    assert processor.get_completed_proposal(delivery_id="att1-728") is not None
    assert len(requests) == 2
    first, second = (r["messages"] for r in requests)
    assert len(second) == len(first) + 1
    correction = second[-1]
    assert correction["role"] == "user"
    assert "1086 caracteres" in correction["content"]
    assert "a" * 50 not in correction["content"]
    assert [r.getMessage() for r in caplog.records] == [
        "agent_output_invalid attempt=1 rule=reply_too_long conversation=728"
    ]


def test_prose_twice_still_fails_and_logs_without_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Maribel #39: prosa sin JSON las dos veces -> invalid_agent_output."""
    processor, requests = _processor(tmp_path, ["Creo que no es fraude, mira."])

    with caplog.at_level(logging.INFO, logger="bridge.hermes"):
        _run(processor, "maribel-39", ref="39")

    assert processor.get_completed_proposal(delivery_id="maribel-39") is None
    assert processor.get_failure_reason(delivery_id="maribel-39") == "invalid_agent_output"
    assert len(requests) == 2
    assert "no era un objeto JSON" in requests[1]["messages"][-1]["content"]
    logged = [r.getMessage() for r in caplog.records]
    assert logged == [
        "agent_output_invalid attempt=1 rule=not_json conversation=39",
        "agent_output_invalid attempt=2 rule=not_json conversation=39",
    ]
    assert all("fraude" not in line for line in logged)


def test_the_first_attempt_carries_no_correction(tmp_path: Path) -> None:
    processor, requests = _processor(tmp_path, [json.dumps(_proposal())])
    _run(processor, "sin-correccion")
    assert [m["role"] for m in requests[0]["messages"]] == ["user"]


def test_the_log_event_is_reviewed() -> None:
    assert "agent_output_invalid" in LOGGED_INFO_EVENTS
