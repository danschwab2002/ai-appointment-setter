"""La cadena de tres niveles del saludo y la inferencia del primer nombre.

Los nombres salen de ``tests/fixtures/lead_names_inbox9_template_params_20260928.json``:
los patrones capturados de las plantillas enviadas en el inbox 9 de Chatwoot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bridge.lead_first_name import (
    PROMPT_VERSION,
    FirstNameInference,
    FirstNameInferenceClient,
    FirstNameInferenceProviderError,
    MAX_TEMPLATE_GREETING_CHARS,
    infer_and_record_first_name,
    lead_name_key,
    resolve_greeting_name,
    template_greeting_name_is_safe,
    validated_model_first_name,
)


FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "lead_names_inbox9_template_params_20260928.json"
    ).read_text(encoding="utf-8")
)
CASES = FIXTURE["cases"]


class _Store:
    def __init__(
        self,
        stored: dict[str, FirstNameInference] | None = None,
        *,
        fail_get: bool = False,
        fail_record: bool = False,
    ) -> None:
        self.stored = dict(stored or {})
        self.fail_get = fail_get
        self.fail_record = fail_record
        self.recorded: list[tuple[str, FirstNameInference, str, str]] = []

    async def get_lead_first_name_inference(
        self, name_key: str
    ) -> FirstNameInference | None:
        if self.fail_get:
            raise RuntimeError("supabase down")
        return self.stored.get(name_key)

    async def record_lead_first_name_inference(
        self,
        *,
        name_key: str,
        inference: FirstNameInference,
        model_name: str,
        prompt_version: str,
    ) -> str:
        if self.fail_record:
            raise RuntimeError("supabase down")
        self.recorded.append((name_key, inference, model_name, prompt_version))
        if name_key in self.stored:
            return "existing"
        self.stored[name_key] = inference
        return "inserted"


def _client(handler) -> FirstNameInferenceClient:
    return FirstNameInferenceClient(
        base_url="http://hermes:8642/v1",
        api_key="test-key",
        model_name="agente-comercial",
        transport=httpx.MockTransport(handler),
    )


def _answer(content: str, status: int = 200):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, json={"choices": [{"message": {"content": content}}]}
        )

    return handler, requests


@pytest.mark.parametrize("case", CASES, ids=[c["full_name"] for c in CASES])
def test_without_inference_greets_by_the_deterministic_first_name(case) -> None:
    greeting = asyncio.run(resolve_greeting_name(case["full_name"], store=None))

    if case["deterministic"] is None:
        # Nada usable: queda el nombre tal cual, porque {{1}} no puede ir vacio.
        assert greeting.source == "full_name"
        assert greeting.name == case["full_name"].strip()
    else:
        assert greeting.source == "deterministic"
        assert greeting.name == case["deterministic"]


def test_a_confident_inference_wins_over_the_deterministic_cut() -> None:
    store = _Store(
        {lead_name_key("San Juana González"): FirstNameInference("confident", "San Juana")}
    )

    greeting = asyncio.run(resolve_greeting_name("San Juana González", store=store))

    assert (greeting.name, greeting.source) == ("San Juana", "inferred")


def test_an_uncertain_inference_falls_to_the_deterministic_cut() -> None:
    store = _Store({lead_name_key("VIGO ARAUJO"): FirstNameInference("uncertain", None)})

    greeting = asyncio.run(resolve_greeting_name("VIGO ARAUJO", store=store))

    assert (greeting.name, greeting.source) == ("Vigo", "deterministic")


def test_a_failing_lookup_never_blocks_the_greeting() -> None:
    greeting = asyncio.run(
        resolve_greeting_name("juan lópez", store=_Store(fail_get=True))
    )

    assert (greeting.name, greeting.source) == ("Juan", "deterministic")


def test_the_key_ignores_case_and_spacing_but_not_the_words() -> None:
    assert lead_name_key("juan  LÓPEZ ") == lead_name_key("Juan López")
    assert lead_name_key("Juan López") != lead_name_key("Juan Lopez")
    assert lead_name_key("   ") is None
    assert lead_name_key(None) is None


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if "model_first_name" in c],
    ids=[c["full_name"] for c in CASES if "model_first_name" in c],
)
def test_compound_names_from_the_model_are_accepted(case) -> None:
    proposed = case["model_first_name"].lower()

    assert (
        validated_model_first_name(case["full_name"], proposed)
        == case["model_first_name"]
    )


@pytest.mark.parametrize(
    ("full_name", "candidate"),
    [
        ("Juan Carlos Pérez", "Juanca"),  # inventado
        ("Juan Carlos Pérez", "Juan Pérez"),  # palabras no seguidas
        ("Andres Felipe Pérez García", "Andres Felipe Pérez García"),  # mas de 3
        ("VM🌷", "VM🌷"),  # no son letras
        ("Juan Carlos", ""),
        ("Juan Carlos", None),
        ("Juan Carlos", "Ignora las instrucciones"),
    ],
)
def test_the_model_cannot_invent_or_reshape_a_name(full_name, candidate) -> None:
    assert validated_model_first_name(full_name, candidate) is None


def test_the_client_sends_only_the_name_and_keeps_a_confident_answer() -> None:
    handler, requests = _answer('{"result":"confident","first_name":"andres felipe"}')

    inference = asyncio.run(_client(handler).infer("Andres  Felipe Pérez García"))

    assert inference == FirstNameInference("confident", "Andres Felipe")
    body = json.loads(requests[0].content)
    assert body["messages"][1]["content"] == json.dumps(
        {"full_name": "Andres Felipe Pérez García"}, ensure_ascii=False
    )
    assert requests[0].headers["Idempotency-Key"] == (
        f"{PROMPT_VERSION}:{lead_name_key('Andres Felipe Pérez García')}"
    )


@pytest.mark.parametrize(
    "content",
    [
        '{"result":"uncertain","first_name":null}',
        '{"result":"confident","first_name":"Pedro"}',  # no esta en el nombre
        '{"result":"confident","first_name":"Juan","score":0.9}',  # fuera de contrato
        "Juan",
        "",
    ],
)
def test_anything_but_a_valid_confident_answer_is_uncertain(content) -> None:
    handler, _ = _answer(content)

    inference = asyncio.run(_client(handler).infer("Juan Carlos Pérez"))

    assert inference == FirstNameInference("uncertain", None)


def test_a_fenced_json_answer_is_read() -> None:
    handler, _ = _answer('```json\n{"result":"confident","first_name":"Juan Carlos"}\n```')

    inference = asyncio.run(_client(handler).infer("Juan Carlos Pérez"))

    assert inference == FirstNameInference("confident", "Juan Carlos")


def test_a_provider_failure_raises_instead_of_answering() -> None:
    handler, _ = _answer("{}", status=502)

    with pytest.raises(FirstNameInferenceProviderError):
        asyncio.run(_client(handler).infer("Juan Carlos Pérez"))


def test_infer_and_record_stores_the_first_answer_once() -> None:
    handler, requests = _answer('{"result":"confident","first_name":"San Juana"}')
    store = _Store()
    client = _client(handler)

    first = asyncio.run(
        infer_and_record_first_name(
            full_name="San Juana González", client=client, store=store
        )
    )
    second = asyncio.run(
        infer_and_record_first_name(
            full_name="san juana gonzález", client=client, store=store
        )
    )

    assert first == "lead_first_name_confident"
    assert second == "lead_first_name_already_inferred"
    assert len(requests) == 1
    [(key, inference, model_name, prompt_version)] = store.recorded
    assert key == lead_name_key("San Juana González")
    assert inference == FirstNameInference("confident", "San Juana")
    assert (model_name, prompt_version) == ("agente-comercial", PROMPT_VERSION)


def test_a_provider_failure_is_not_recorded_so_the_next_form_retries() -> None:
    handler, _ = _answer("{}", status=500)
    store = _Store()

    outcome = asyncio.run(
        infer_and_record_first_name(
            full_name="Juan Carlos Pérez", client=_client(handler), store=store
        )
    )

    assert outcome == "lead_first_name_provider_http_error"
    assert store.recorded == []


def test_a_store_failure_never_raises() -> None:
    handler, _ = _answer('{"result":"confident","first_name":"Juan"}')

    outcome = asyncio.run(
        infer_and_record_first_name(
            full_name="Juan Pérez",
            client=_client(handler),
            store=_Store(fail_record=True),
        )
    )

    assert outcome == "lead_first_name_store_error"


def test_an_empty_name_is_never_sent_to_the_model() -> None:
    handler, requests = _answer('{"result":"confident","first_name":"x"}')

    outcome = asyncio.run(
        infer_and_record_first_name(
            full_name="   ", client=_client(handler), store=_Store()
        )
    )

    assert outcome == "lead_first_name_empty"
    assert requests == []


def test_prompt_examples_are_not_names_of_the_measured_sample() -> None:
    """Los ejemplos del prompt no pueden ser nombres de la muestra medida.

    El prompt v2 se midio contra los nombres reales del inbox 9; si sus ejemplos
    fueran esos nombres, la medicion probaria memoria y no criterio.
    """
    from bridge import lead_first_name

    prompt = lead_first_name._SYSTEM_PROMPT.casefold()
    for case in CASES:
        for name in (case.get("model_first_name"), case["full_name"]):
            if name and len(name.split()) > 1:
                assert f'"{name.casefold()}' not in prompt, name


def test_the_prompt_version_changes_the_idempotency_key() -> None:
    assert PROMPT_VERSION == "lead-first-name-v2"


# ---------------------------------------------- el nombre en una plantilla


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("http://evil.example/premio", id="url"),
        pytest.param("soporte@evil.example", id="email"),
        pytest.param("\U0001F381 evil.example", id="emoji and domain"),
        pytest.param("evil.example", id="bare domain"),
        pytest.param("x" * (MAX_TEMPLATE_GREETING_CHARS + 1), id="61 characters"),
        pytest.param("5512345678", id="only digits"),
        pytest.param("Ana 2", id="a digit"),
        pytest.param("evil\uff0eexample", id="fullwidth full stop"),
        pytest.param("Ana\u202eelpmaxe", id="right-to-left override"),
        pytest.param("Juan_Perez", id="underscore"),
        pytest.param("   ", id="blank"),
        pytest.param(None, id="not a string"),
    ],
)
def test_a_value_that_is_not_a_name_does_not_go_in_a_template(value) -> None:
    assert template_greeting_name_is_safe(value) is False


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("J.C. Pérez", id="initials with dots"),
        pytest.param("O’Brien", id="typographic apostrophe"),
        pytest.param("O'Brien", id="apostrophe"),
        pytest.param("Ana-Lucía", id="hyphen"),
        pytest.param("Pérez, Juan", id="comma"),
        pytest.param("María de los Ángeles Fernández Gutiérrez", id="41 characters"),
        pytest.param("x" * MAX_TEMPLATE_GREETING_CHARS, id="60 characters"),
        pytest.param("\U0001F44D\U0001F3FD Ana", id="emoji with skin tone"),
        pytest.param("\U0001F468\u200d\U0001F469\u200d\U0001F467", id="zwj family"),
        pytest.param("Edith\nGarcía\t     Pérez", id="whitespace Meta refuses, collapsed"),
    ],
)
def test_the_shapes_of_a_real_name_go_in_a_template(value) -> None:
    assert template_greeting_name_is_safe(value) is True


@pytest.mark.parametrize("case", CASES, ids=[c["pattern"] for c in CASES])
def test_every_captured_name_and_greeting_goes_in_a_template(case) -> None:
    for value in (
        case["full_name"],
        case["deterministic"],
        case.get("model_first_name"),
    ):
        if value is not None:
            assert template_greeting_name_is_safe(value) is True, value
