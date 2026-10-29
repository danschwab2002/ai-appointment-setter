import asyncio
import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from bridge.reply_splitter import (
    MAX_REPLY_PARTS,
    SHORT_REPLY_MAX_CHARS,
    HermesReplySplitter,
    ReplySplitManifestConflictError,
    ReplySplitManifestStorageError,
    validate_reply_parts,
)

# Respuestas sintéticas, sin datos personales. Las largas pasan el umbral de
# SHORT_REPLY_MAX_CHARS y llegan al modelo; la corta tiene la forma de la que
# el modelo partió en 3 globos en Maribel (29/10), de unos 75 caracteres.
_CUOTAS = (
    "Sí, podés pagar en cuotas: el checkout te muestra las opciones de tu "
    "tarjeta antes de confirmar."
)
_PAIS = (
    "¿Desde qué país operás? Así te confirmo qué medios de pago te aparecen "
    "y si te conviene pagar en tu moneda."
)
_PRIMERA = (
    "Primera parte de una respuesta larga, con el detalle del programa, "
    "cómo se accede y qué incluye."
)
_SEGUNDA = (
    "Segunda parte, con la pregunta que sigue para entender qué necesitás "
    "antes de pasarte el enlace de pago."
)
_LARGA = f"{_PRIMERA} {_SEGUNDA}"
_CORTA_DE_MARIBEL = (
    "Hola, un gusto. Soy del equipo de la academia, cuéntame. ¿Qué duda tienes?"
)


def test_splits_reply_with_semantic_batch_identity_and_reuses_the_manifest(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "parts": [_CUOTAS, _PAIS]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )
    reply = f"{_CUOTAS} {_PAIS}"

    first = asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    )

    assert first == (_CUOTAS, _PAIS)
    assert replay == first
    assert len(requests) == 1
    request_body = json.loads(requests[0].content)
    assert request_body["provider"] == "small-provider"
    assert request_body["model"] == "small-model"
    assert request_body["stream"] is False
    assert request_body["messages"][0]["role"] == "system"
    assert request_body["messages"][1] == {
        "role": "user",
        "content": json.dumps(
            {"reply": reply, "max_parts": 3},
            ensure_ascii=False,
        ),
    }
    result_path = next((tmp_path / "splits").glob("*.json"))
    manifest = json.loads(result_path.read_text(encoding="utf-8"))
    batch_hash = hashlib.sha256(b"123:789").hexdigest()
    assert manifest == {
        "manifest_version": 1,
        "status": "completed",
        "batch_hash": batch_hash,
        "reply_hash": hashlib.sha256(reply.encode("utf-8")).hexdigest(),
        "part_count": 2,
        "parts": [
            {
                "index": 1,
                "content": _CUOTAS,
                "content_hash": hashlib.sha256(
                    _CUOTAS.encode("utf-8")
                ).hexdigest(),
                "part_hash": hashlib.sha256(
                    f"{batch_hash}:1:2".encode("utf-8")
                ).hexdigest(),
            },
            {
                "index": 2,
                "content": _PAIS,
                "content_hash": hashlib.sha256(
                    _PAIS.encode("utf-8")
                ).hexdigest(),
                "part_hash": hashlib.sha256(
                    f"{batch_hash}:2:2".encode("utf-8")
                ).hexdigest(),
            },
        ],
    }
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "splits").stat().st_mode) == 0o700


def test_missing_manifest_after_batch_claim_fails_closed_without_resplitting(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"parts": [_PRIMERA, _SEGUNDA]},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    result_dir = tmp_path / "splits"
    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=result_dir,
        transport=httpx.MockTransport(handler),
    )
    reply = f"{_PRIMERA} {_SEGUNDA}"
    assert asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    ) == (_PRIMERA, _SEGUNDA)

    batch_hash = hashlib.sha256(b"123:789").hexdigest()
    claim_path = tmp_path / f".{batch_hash}.reply-batch.claim"
    assert claim_path.read_text(encoding="utf-8") == "claimed\n"
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
    (result_dir / f"{batch_hash}.json").unlink()

    with pytest.raises(
        ReplySplitManifestStorageError,
        match="reply_split_manifest_missing_after_claim",
    ):
        asyncio.run(
            splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
        )

    assert calls == 1


def test_invalid_or_rewritten_split_falls_back_to_the_original_reply(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"parts": [_CUOTAS, "Texto inventado"]},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )
    reply = f"{_CUOTAS} {_PAIS}"

    first = asyncio.run(
        splitter.split(conversation_id=2, trigger_message_id=20, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=2, trigger_message_id=20, reply=reply)
    )

    assert first == (reply,)
    assert replay == (reply,)
    assert calls == 1


def test_existing_semantic_manifest_rejects_a_different_logical_reply(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"parts": [_PRIMERA, _SEGUNDA]},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )

    first = asyncio.run(
        splitter.split(
            conversation_id=123,
            trigger_message_id=789,
            reply=f"{_PRIMERA} {_SEGUNDA}",
        )
    )

    async def replay_with_changed_reply() -> None:
        await splitter.split(
            conversation_id=123,
            trigger_message_id=789,
            reply="Una respuesta distinta.",
        )

    try:
        asyncio.run(replay_with_changed_reply())
    except ReplySplitManifestConflictError:
        pass
    else:
        raise AssertionError("expected immutable reply manifest conflict")

    assert first == (_PRIMERA, _SEGUNDA)
    assert calls == 1


def test_accepts_a_complete_json_markdown_fence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                + json.dumps(
                                    {"parts": [_PRIMERA, _SEGUNDA]},
                                    ensure_ascii=False,
                                )
                                + "\n```"
                            )
                        }
                    }
                ]
            },
        )

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        splitter.split(
            conversation_id=3,
            trigger_message_id=30,
            reply=f"{_PRIMERA} {_SEGUNDA}",
        )
    )

    assert result == (_PRIMERA, _SEGUNDA)


def test_http_failure_falls_back_once_and_is_reused_on_replay(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )
    reply = f"{_PRIMERA} {_SEGUNDA}"

    first = asyncio.run(
        splitter.split(conversation_id=4, trigger_message_id=40, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=4, trigger_message_id=40, reply=reply)
    )

    assert first == (reply,)
    assert replay == (reply,)
    assert calls == 1


def test_internal_whitespace_changes_are_rejected(tmp_path: Path) -> None:
    proposals = iter(
        (
            json.dumps({"parts": [f"{_LARGA} B"]}, ensure_ascii=False),
            json.dumps({"parts": [f"{_LARGA} B"]}, ensure_ascii=False),
        )
    )

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(proposals)}}]},
        )

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )

    repeated_space = asyncio.run(
        splitter.split(
            conversation_id=5, trigger_message_id=50, reply=f"{_LARGA}  B"
        )
    )
    newline = asyncio.run(
        splitter.split(
            conversation_id=5, trigger_message_id=51, reply=f"{_LARGA}\nB"
        )
    )

    assert repeated_space == (f"{_LARGA}  B",)
    assert newline == (f"{_LARGA}\nB",)
    assert calls == 2


def test_symlinked_cache_directory_fails_closed_without_writing_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    result_dir = tmp_path / "splits"
    result_dir.symlink_to(outside, target_is_directory=True)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=result_dir,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ReplySplitManifestStorageError):
        asyncio.run(
            splitter.split(
                conversation_id=6,
                trigger_message_id=60,
                reply="Original.",
            )
        )

    assert calls == 0
    assert list(outside.iterdir()) == []


def test_malformed_existing_cache_fails_closed(tmp_path: Path) -> None:
    result_dir = tmp_path / "splits"
    result_dir.mkdir(mode=0o700)
    digest = hashlib.sha256(b"7:70").hexdigest()
    (result_dir / f"{digest}.json").write_text("not-json", encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=result_dir,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ReplySplitManifestStorageError):
        asyncio.run(
            splitter.split(
                conversation_id=7,
                trigger_message_id=70,
                reply="Original.",
            )
        )

    assert calls == 0


def test_application_boundary_rejects_a_non_sequence_result() -> None:
    assert validate_reply_parts("Original.", object()) is None


def _splitter_with(
    tmp_path: Path,
    proposal: list[str],
    requests: list[httpx.Request],
) -> HermesReplySplitter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"parts": proposal}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    return HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="hermes-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=tmp_path / "splits",
        transport=httpx.MockTransport(handler),
    )


def test_short_reply_stays_in_one_bubble_without_calling_the_model(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    # Lo que el modelo liviano propuso en Maribel para una respuesta así.
    splitter = _splitter_with(
        tmp_path,
        [
            "Hola, un gusto.",
            "Soy del equipo de la academia, cuéntame.",
            "¿Qué duda tienes?",
        ],
        requests,
    )
    reply = _CORTA_DE_MARIBEL

    first = asyncio.run(
        splitter.split(conversation_id=8, trigger_message_id=80, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=8, trigger_message_id=80, reply=reply)
    )
    loaded = asyncio.run(
        splitter.load_existing(
            conversation_id=8, trigger_message_id=80, reply=reply
        )
    )

    assert first == (reply,)
    assert replay == (reply,)
    assert loaded == (reply,)
    assert requests == []
    batch_hash = hashlib.sha256(b"8:80").hexdigest()
    manifest = json.loads(
        (tmp_path / "splits" / f"{batch_hash}.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "fallback"
    assert manifest["reason"] == "short_reply"
    assert manifest["part_count"] == 1
    assert manifest["parts"][0]["part_hash"] == batch_hash
    claim_path = tmp_path / f".{batch_hash}.reply-batch.claim"
    assert claim_path.read_text(encoding="utf-8") == "claimed\n"


def test_short_reply_threshold_is_160_characters(tmp_path: Path) -> None:
    under: list[httpx.Request] = []
    at: list[httpx.Request] = []
    reply_under = "a" * (SHORT_REPLY_MAX_CHARS - 1)
    reply_at = "a" * (SHORT_REPLY_MAX_CHARS - 2) + " b"

    under_result = asyncio.run(
        _splitter_with(tmp_path / "under", ["a"], under).split(
            conversation_id=9, trigger_message_id=90, reply=reply_under
        )
    )
    at_result = asyncio.run(
        _splitter_with(
            tmp_path / "at", ["a" * (SHORT_REPLY_MAX_CHARS - 2), "b"], at
        ).split(conversation_id=9, trigger_message_id=91, reply=reply_at)
    )

    assert SHORT_REPLY_MAX_CHARS == 160
    assert under_result == (reply_under,)
    assert under == []
    assert at_result == ("a" * (SHORT_REPLY_MAX_CHARS - 2), "b")
    assert len(at) == 1


def test_long_reply_accepts_three_parts_and_asks_for_three_at_most(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    splitter = _splitter_with(tmp_path, [_CUOTAS, _PRIMERA, _PAIS], requests)
    reply = f"{_CUOTAS} {_PRIMERA} {_PAIS}"

    result = asyncio.run(
        splitter.split(conversation_id=10, trigger_message_id=100, reply=reply)
    )

    assert result == (_CUOTAS, _PRIMERA, _PAIS)
    assert MAX_REPLY_PARTS == 3
    body = json.loads(requests[0].content)
    assert json.loads(body["messages"][1]["content"]) == {
        "reply": reply,
        "max_parts": 3,
    }
    assert "between 1 and 3" in body["messages"][0]["content"]
    assert "4" not in body["messages"][0]["content"]


def test_long_reply_split_in_four_falls_back_to_one_bubble(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    splitter = _splitter_with(
        tmp_path, [_CUOTAS, _PRIMERA, _SEGUNDA, _PAIS], requests
    )
    reply = f"{_CUOTAS} {_PRIMERA} {_SEGUNDA} {_PAIS}"

    first = asyncio.run(
        splitter.split(conversation_id=11, trigger_message_id=110, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=11, trigger_message_id=110, reply=reply)
    )

    assert first == (reply,)
    assert replay == (reply,)
    assert len(requests) == 1
    batch_hash = hashlib.sha256(b"11:110").hexdigest()
    manifest = json.loads(
        (tmp_path / "splits" / f"{batch_hash}.json").read_text(encoding="utf-8")
    )
    assert manifest["reason"] == "invalid_splitter_output"


def _write_legacy_manifest(
    tmp_path: Path,
    *,
    conversation_id: int,
    trigger_message_id: int,
    reply: str,
    parts: tuple[str, ...],
) -> None:
    result_dir = tmp_path / "splits"
    result_dir.mkdir(mode=0o700)
    digest = hashlib.sha256(
        f"{conversation_id}:{trigger_message_id}".encode("utf-8")
    ).hexdigest()
    manifest = HermesReplySplitter._manifest(
        status="completed",
        batch_hash=digest,
        reply_hash=hashlib.sha256(reply.encode("utf-8")).hexdigest(),
        parts=parts,
    )
    path = result_dir / f"{digest}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def test_manifest_with_four_parts_persisted_before_the_cap_still_replays(
    tmp_path: Path,
) -> None:
    parts = (_CUOTAS, _PRIMERA, _SEGUNDA, _PAIS)
    reply = " ".join(parts)
    _write_legacy_manifest(
        tmp_path,
        conversation_id=12,
        trigger_message_id=120,
        reply=reply,
        parts=parts,
    )
    requests: list[httpx.Request] = []
    splitter = _splitter_with(tmp_path, [reply], requests)

    replay = asyncio.run(
        splitter.split(conversation_id=12, trigger_message_id=120, reply=reply)
    )
    loaded = asyncio.run(
        splitter.load_existing(
            conversation_id=12, trigger_message_id=120, reply=reply
        )
    )

    assert replay == parts
    assert loaded == parts
    assert validate_reply_parts(reply, parts) == parts
    assert requests == []


def test_short_reply_already_split_before_the_change_keeps_its_manifest(
    tmp_path: Path,
) -> None:
    parts = (
        "Hola, un gusto.",
        "Soy del equipo de la academia, cuéntame.",
        "¿Qué duda tienes?",
    )
    reply = _CORTA_DE_MARIBEL
    _write_legacy_manifest(
        tmp_path,
        conversation_id=13,
        trigger_message_id=130,
        reply=reply,
        parts=parts,
    )
    requests: list[httpx.Request] = []
    splitter = _splitter_with(tmp_path, [reply], requests)

    replay = asyncio.run(
        splitter.split(conversation_id=13, trigger_message_id=130, reply=reply)
    )

    assert replay == parts
    assert requests == []
