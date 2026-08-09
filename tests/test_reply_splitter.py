import asyncio
import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from bridge.reply_splitter import (
    HermesReplySplitter,
    ReplySplitManifestConflictError,
    ReplySplitManifestStorageError,
    validate_reply_parts,
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
                                    "parts": [
                                        "Sí, podés pagar en cuotas.",
                                        "¿Desde qué país operás?",
                                    ]
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
    reply = "Sí, podés pagar en cuotas. ¿Desde qué país operás?"

    first = asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    )
    replay = asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    )

    assert first == (
        "Sí, podés pagar en cuotas.",
        "¿Desde qué país operás?",
    )
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
            {"reply": reply, "max_parts": 4},
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
                "content": "Sí, podés pagar en cuotas.",
                "content_hash": hashlib.sha256(
                    "Sí, podés pagar en cuotas.".encode("utf-8")
                ).hexdigest(),
                "part_hash": hashlib.sha256(
                    f"{batch_hash}:1:2".encode("utf-8")
                ).hexdigest(),
            },
            {
                "index": 2,
                "content": "¿Desde qué país operás?",
                "content_hash": hashlib.sha256(
                    "¿Desde qué país operás?".encode("utf-8")
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
                            "content": '{"parts":["Primera parte.","Segunda parte."]}'
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
    reply = "Primera parte. Segunda parte."
    assert asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    ) == ("Primera parte.", "Segunda parte.")

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
                            "content": '{"parts":["Sí, claro.","Texto inventado"]}'
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
    reply = "Sí, claro. Te explico cómo funciona."

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
                            "content": '{"parts":["Primera.","Segunda."]}'
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
            reply="Primera. Segunda.",
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

    assert first == ("Primera.", "Segunda.")
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
                                '{"parts":["Primera.","Segunda."]}'
                                "\n```"
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
            reply="Primera. Segunda.",
        )
    )

    assert result == ("Primera.", "Segunda.")


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
    reply = "Respuesta comercial válida."

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
            '{"parts":["A B"]}',
            '{"parts":["A B"]}',
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
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
        splitter.split(conversation_id=5, trigger_message_id=50, reply="A  B")
    )
    newline = asyncio.run(
        splitter.split(conversation_id=5, trigger_message_id=51, reply="A\nB")
    )

    assert repeated_space == ("A  B",)
    assert newline == ("A\nB",)


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
