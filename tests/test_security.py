from bridge.security import verify_chatwoot_signature


def test_accepts_valid_chatwoot_signature() -> None:
    raw_body = b'{"event":"message_created"}'

    assert verify_chatwoot_signature(
        raw_body=raw_body,
        timestamp="1700000000",
        received_signature=(
            "sha256=9dcdcaaadf7652a518bc7d9ec7403c6c"
            "bae2abfba9cf20f561683beec1e34b81"
        ),
        secret="test-secret",
    )
