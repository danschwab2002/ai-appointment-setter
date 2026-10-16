"""El producto en el pedido del link (varios productos por aliada, paso 2b).

Decision de Dan del 2026-10-16: cuando pide el link, el agente dice de que
producto es, y el bridge solo acepta productos declarados. Con [[productos]] en
el manifiesto la salida del agente admite una septima clave, `product`; sin
productos, la salida es la de siempre y Johanna no cambia.

Fixture: tests/fixtures/hermes_glm_product_link_20261016.json, respuestas reales
de z-ai/glm-5.3-flash con el SOUL comun de esta rama y el conocimiento v8 de
ATT1, pedidas con el arnes de la instancia.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from bridge.hermes import HermesShadowProcessor, _is_valid_proposal, payment_link_product
from bridge.supabase import PersonActivity
from test_hermes import _valid_proposal
from test_person_activity import _products_settings
from test_whatsapp_inbound_equivalence import (
    PAYMENT_LINK_PROPOSAL,
    _PaymentLinkSupabase,
    _process,
    _text_webhook,
)

ATT1 = frozenset({"att1"})
TWO = frozenset({"att1", "att2"})


def _link(**overrides: object) -> dict[str, object]:
    proposal = {
        **_valid_proposal(),
        "decision": "send_payment_link",
        "reason_code": "payment_link_requested",
        "reply": "Te paso el enlace para inscribirte:",
    }
    proposal.update(overrides)
    return proposal


# ------------------------------------------------------- la validacion


def test_without_products_the_seven_keys_are_still_invalid() -> None:
    # Johanna, y ATT1 antes de declarar [[productos]].
    assert _is_valid_proposal(_link())
    assert not _is_valid_proposal(_link(product="att1"))
    assert not _is_valid_proposal(_link(product=None))


@pytest.mark.parametrize("product", ["att1", None])
def test_with_products_the_product_is_a_declared_key_or_null(product: object) -> None:
    assert _is_valid_proposal(_link(product=product), ATT1)
    assert _is_valid_proposal({**_valid_proposal(), "product": product}, ATT1)


@pytest.mark.parametrize("product", ["att2", "ATT1", "", 1, True, ["att1"], {"key": "att1"}])
def test_an_undeclared_or_malformed_product_invalidates_the_proposal(product: object) -> None:
    assert not _is_valid_proposal(_link(product=product), ATT1)
    assert not _is_valid_proposal({**_valid_proposal(), "product": product}, ATT1)


def test_with_one_product_the_six_keys_are_still_valid() -> None:
    # Un SOUL que todavia no conoce `product` no deja a nadie sin link.
    assert _is_valid_proposal(_link(), ATT1)
    assert _is_valid_proposal(_valid_proposal(), ATT1)


def test_with_two_products_a_link_without_product_is_invalid() -> None:
    assert not _is_valid_proposal(_link(), TWO)
    assert not _is_valid_proposal(_link(product=None), TWO)
    assert _is_valid_proposal(_link(product="att2"), TWO)
    # Solo el link necesita el producto: una respuesta comun no.
    assert _is_valid_proposal(_valid_proposal(), TWO)


def test_any_other_extra_key_is_still_invalid() -> None:
    assert not _is_valid_proposal({**_link(), "producto": "att1"}, ATT1)
    assert not _is_valid_proposal({**_link(), "product": "att1", "offer": "x"}, ATT1)


def test_the_link_product_is_the_one_the_agent_named() -> None:
    assert payment_link_product(_link(product="att2"), ("att1", "att2")) == "att2"


def test_with_one_product_and_no_name_the_link_is_of_that_product() -> None:
    assert payment_link_product(_link(), ("att1",)) == "att1"
    assert payment_link_product(_link(product=None), ("att1",)) == "att1"


def test_without_products_the_link_has_no_product() -> None:
    assert payment_link_product(_link(), ()) is None


# ------------------------------------------- el procesador con el modelo real


CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "hermes_glm_product_link_20261016.json").read_text(
        encoding="utf-8"
    )
)


def _content(case: str) -> str:
    [response] = [item for item in CAPTURE["responses"] if item["case"] == case]
    return response["content"]


def _processor(tmp_path: Path, content: str, **kwargs: object) -> tuple[HermesShadowProcessor, list[str]]:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    return processor, keys


def _run(processor: HermesShadowProcessor, delivery_id: str) -> None:
    asyncio.run(
        processor.run(delivery_id=delivery_id, context={"conversation_ref": "prueba-2b", "messages": []})
    )


def test_the_real_link_request_with_its_product_completes_and_is_kept(tmp_path: Path) -> None:
    """La respuesta real del modelo con el SOUL de la 2b: pide el link de att1."""
    processor, keys = _processor(
        tmp_path, _content("01-enlace-con-formulario"), product_keys=("att1",)
    )

    _run(processor, "link-att1")

    proposal = processor.get_completed_proposal(delivery_id="link-att1")
    assert proposal is not None
    assert proposal["decision"] == "send_payment_link"
    assert proposal["product"] == "att1"
    assert payment_link_product(proposal, ("att1",)) == "att1"
    # Guardado y terminal: el reproceso de la misma entrega no vuelve a pedir.
    assert processor.has_result(delivery_id="link-att1")
    _run(processor, "link-att1")
    assert len(keys) == 1


def test_without_products_the_same_real_answer_is_invalid(tmp_path: Path) -> None:
    processor, keys = _processor(tmp_path, _content("01-enlace-con-formulario"))

    _run(processor, "link-sin-productos")

    assert processor.get_completed_proposal(delivery_id="link-sin-productos") is None
    assert processor.has_result(delivery_id="link-sin-productos")
    assert len(keys) == 2  # el reintento, y despues invalid_agent_output


@pytest.mark.parametrize("case", ["03-pregunta-programa", "06-sin-productos"])
def test_the_real_six_key_answers_are_valid_with_and_without_products(
    case: str, tmp_path: Path
) -> None:
    for product_keys in ((), ("att1",)):
        folder = tmp_path / (product_keys[0] if product_keys else "none")
        processor, _ = _processor(folder, _content(case), product_keys=product_keys)

        _run(processor, case)

        proposal = processor.get_completed_proposal(delivery_id=case)
        assert proposal is not None
        assert "product" not in proposal


# --------------------------------------------------------- el bridge


class _LinkSupabase(_PaymentLinkSupabase):
    """La emision del enlace y la lectura de lo que hizo la persona."""

    async def get_portable_person_activity(self, **kwargs: object) -> tuple[PersonActivity, ...]:
        return ()


def _link_turn(tmp_path: Path, proposal: dict[str, object], settings):
    webhook, history = _text_webhook("Envíame el enlace")
    supabase = _LinkSupabase()
    chatwoot, _, _ = _process(
        tmp_path,
        supabase,
        settings=replace(settings, payment_link_enabled=True),
        webhook=webhook,
        history=history,
        proposal=proposal,
    )
    return chatwoot, supabase


def test_the_link_the_agent_asked_for_logs_its_product_and_goes_out_as_always(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        chatwoot, supabase = _link_turn(
            tmp_path,
            {**PAYMENT_LINK_PROPOSAL, "product": "att1"},
            _products_settings(tmp_path),
        )

    assert "payment_link_product product=att1 source=agent" in caplog.text
    [reservation] = supabase.candidate_calls
    assert reservation["chatwoot_conversation_id"] == 200
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])


def test_a_link_without_product_is_of_the_only_product(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        chatwoot, _ = _link_turn(
            tmp_path, dict(PAYMENT_LINK_PROPOSAL), _products_settings(tmp_path)
        )

    assert "payment_link_product product=att1 source=single_product" in caplog.text
    assert chatwoot.reply_calls


def test_without_productos_the_link_logs_no_product(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from test_whatsapp_inbound_equivalence import _manifest_settings

    with caplog.at_level(logging.INFO):
        chatwoot, _ = _link_turn(
            tmp_path, dict(PAYMENT_LINK_PROPOSAL), _manifest_settings(tmp_path)
        )

    assert "payment_link_product" not in caplog.text
    assert chatwoot.reply_calls
