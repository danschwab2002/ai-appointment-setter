import asyncio
import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from bridge.hermes import HermesShadowProcessor, _is_valid_proposal


def _valid_proposal() -> dict[str, object]:
    return {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "need_location",
        "reply": "¿En qué ciudad estás?",
        "captured_fields": {
            "person_name": "Juan",
            "location": None,
            "role": "Dueño",
            "company_name": "Acme",
            "company_size": 5,
            "business_model": "Servicios",
            "company_operational": True,
            "can_invest_in_education": None,
        },
        "missing_fields": ["location"],
    }


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("person_name", True),
        ("location", 42),
        ("role", False),
        ("company_name", 12),
        ("company_size", True),
        ("company_size", 0),
        ("business_model", 3),
        ("company_operational", "yes"),
        ("can_invest_in_education", 1),
    ],
)
def test_rejects_invalid_types_for_captured_fields(
    field: str, invalid_value: object
) -> None:
    proposal = _valid_proposal()
    captured = proposal["captured_fields"]
    assert isinstance(captured, dict)
    captured[field] = invalid_value

    assert _is_valid_proposal(proposal) is False


def test_rejects_a_missing_field_that_already_has_a_confirmed_value() -> None:
    proposal = _valid_proposal()
    proposal["missing_fields"] = ["person_name"]

    assert _is_valid_proposal(proposal) is False


def test_rejects_an_unknown_decision_with_a_null_status() -> None:
    proposal = _valid_proposal()
    proposal["decision"] = "unknown"
    proposal["qualification_status"] = None

    assert _is_valid_proposal(proposal) is False


def test_persists_a_valid_hermes_shadow_proposal_privately(tmp_path: Path) -> None:
    context: dict[str, object] = {
        "conversation_ref": "123",
        "human_handoff_confirmed": False,
        "known_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "messages": [{"actor": "prospect", "text": "Hola"}],
    }
    proposal = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "need_person_name",
        "reply": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
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
        "missing_fields": ["person_name"],
    }
    delivery_id = "shadow-delivery"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-hermes-key"
        assert request.headers["Idempotency-Key"] == digest
        body = json.loads(request.content)
        assert body == {
            "model": "agente-comercial",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(proposal, ensure_ascii=False),
                        }
                    }
                ]
            },
        )

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(processor.run(delivery_id=delivery_id, context=context))

    result_path = tmp_path / f"{digest}.json"
    assert json.loads(result_path.read_text()) == {
        "status": "completed",
        "delivery_id_hash": digest,
        "proposal": proposal,
    }
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600

    assert processor.get_completed_proposal(delivery_id=delivery_id) == proposal


def test_does_not_expose_a_failed_result_as_a_sendable_proposal(
    tmp_path: Path,
) -> None:
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
    )
    processor.record_failure(delivery_id="failed", reason="hermes_unavailable")

    assert processor.get_completed_proposal(delivery_id="failed") is None


def test_records_an_unavailable_hermes_service_without_raising(tmp_path: Path) -> None:
    delivery_id = "failed-shadow-delivery"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": "123", "messages": []},
        )
    )

    assert json.loads((tmp_path / f"{digest}.json").read_text()) == {
        "status": "failed",
        "delivery_id_hash": digest,
        "reason": "hermes_unavailable",
    }


def test_rejects_an_incomplete_agent_proposal(tmp_path: Path) -> None:
    delivery_id = "invalid-shadow-delivery"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"decision": "ask_question"}
                            )
                        }
                    }
                ]
            },
        )

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": "123", "messages": []},
        )
    )

    assert json.loads((tmp_path / f"{digest}.json").read_text()) == {
        "status": "failed",
        "delivery_id_hash": digest,
        "reason": "invalid_agent_output",
    }


def test_records_non_json_agent_content_as_invalid(tmp_path: Path) -> None:
    delivery_id = "non-json-shadow-delivery"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "No puedo responder ahora."}}
                ]
            },
        )

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": "123", "messages": []},
        )
    )

    assert json.loads((tmp_path / f"{digest}.json").read_text()) == {
        "status": "failed",
        "delivery_id_hash": digest,
        "reason": "invalid_agent_output",
    }


def test_rejects_a_non_string_missing_field_without_raising() -> None:
    proposal = _valid_proposal()
    proposal["missing_fields"] = [{}]

    assert _is_valid_proposal(proposal) is False


def test_does_not_treat_a_truncated_result_as_terminal(tmp_path: Path) -> None:
    delivery_id = "truncated-result"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    (tmp_path / f"{digest}.json").write_text("{")
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
    )

    assert processor.has_result(delivery_id=delivery_id) is False


def test_does_not_treat_non_utf8_result_as_terminal(tmp_path: Path) -> None:
    delivery_id = "non-utf8-result"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    (tmp_path / f"{digest}.json").write_bytes(b"\xff")
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
    )

    assert processor.has_result(delivery_id=delivery_id) is False


def test_replaces_a_truncated_result_with_a_valid_terminal_result(
    tmp_path: Path,
) -> None:
    delivery_id = "replace-truncated-result"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    result_path = tmp_path / f"{digest}.json"
    result_path.write_text("{")
    proposal = _valid_proposal()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(proposal)}}
                ]
            },
        )

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": "123", "messages": []},
        )
    )

    assert json.loads(result_path.read_text())["status"] == "completed"


def test_concurrent_runs_invoke_hermes_only_once(tmp_path: Path) -> None:
    proposal = _valid_proposal()

    class SlowTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.calls += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps(proposal)}}
                    ]
                },
            )

    transport = SlowTransport()
    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=transport,
    )

    async def run_twice() -> None:
        await asyncio.gather(
            processor.run(delivery_id="concurrent", context={"messages": []}),
            processor.run(delivery_id="concurrent", context={"messages": []}),
        )

    asyncio.run(run_twice())

    assert transport.calls == 1


def test_records_non_utf8_json_response_as_invalid_agent_output(
    tmp_path: Path,
) -> None:
    delivery_id = "non-utf8-agent-response"
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\xff",
            headers={"content-type": "application/json"},
        )

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        processor.run(
            delivery_id=delivery_id,
            context={"conversation_ref": "123", "messages": []},
        )
    )

    assert json.loads((tmp_path / f"{digest}.json").read_text()) == {
        "status": "failed",
        "delivery_id_hash": digest,
        "reason": "invalid_agent_output",
    }
