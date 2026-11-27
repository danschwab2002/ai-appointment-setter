"""El turno de ensayo del agente comercial (``agent_rehearsal``), sin red.

OpenRouter se emula con ``httpx.MockTransport``: la forma del pedido es la de su API de chat
(documentada, https://openrouter.ai/docs/api-reference/chat-completion; no capturada de produccion),
la misma que ya usan ``segment_check.openrouter_judge`` y el arnes de las instancias.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bridge import agent_rehearsal


def test_the_model_comes_from_the_environment_or_from_hermes_toml(tmp_path: Path) -> None:
    assert agent_rehearsal.agent_model(tmp_path, {}) is None
    (tmp_path / "hermes.toml").write_text('[profiles.agente-comercial]\nmodelo = "proveedor/modelo-x"\n',
                                          encoding="utf-8")
    assert agent_rehearsal.agent_model(tmp_path, {}) == "proveedor/modelo-x"
    assert agent_rehearsal.agent_model(tmp_path, {agent_rehearsal.MODEL_ENV: "otro/modelo"}) == "otro/modelo"
    assert agent_rehearsal.agent_model(None, {}) is None


def test_the_request_carries_the_key_in_the_header_and_never_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "proveedor/modelo-x",
                                         "choices": [{"message": {"content": "{}"}}]})

    ask = agent_rehearsal.openrouter_ask(model="proveedor/modelo-x", api_key="k" * 40,
                                         transport=httpx.MockTransport(handler))
    answer = ask([{"role": "user", "content": "hola"}])
    assert answer == agent_rehearsal.Answer(model="proveedor/modelo-x", content="{}")
    [request] = seen
    assert request.headers["authorization"] == "Bearer " + "k" * 40
    body = json.loads(request.content)
    assert body["model"] == "proveedor/modelo-x" and body["stream"] is False and "k" * 40 not in request.content.decode()


def test_an_http_error_is_a_rehearsal_error_without_the_key() -> None:
    ask = agent_rehearsal.openrouter_ask(model="m", api_key="secreta-" + "k" * 30,
                                         transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with pytest.raises(agent_rehearsal.RehearsalError) as error:
        ask([])
    assert "secreta" not in str(error.value)


def test_the_context_has_the_shape_of_the_harness() -> None:
    context = agent_rehearsal.build_context([("prospect", "hola"), ("assistant", "¿Qué buscás?")],
                                            extra={"recent_payment_failure": {"x": 1}}, coupon_applied=True)
    assert [m["actor"] for m in context["messages"]] == ["prospect", "assistant"]
    assert context["payment_link_action"]["coupon_applied"] is True and context["recent_payment_failure"] == {"x": 1}
    with pytest.raises(ValueError):
        agent_rehearsal.build_context([("lead", "hola")])
