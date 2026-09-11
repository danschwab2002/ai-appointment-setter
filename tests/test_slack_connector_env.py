from __future__ import annotations

import json

import pytest

from slack_correlation.app import SlackConnectorSettings, create_app


def test_settings_parse_tenant_scoped_review_base_urls(monkeypatch) -> None:
    monkeypatch.setenv(
        "SLACK_TENANT_REVIEW_BASE_URLS_JSON",
        json.dumps({"johanna": "https://reviews.example.test"}),
    )

    settings = SlackConnectorSettings.from_env()

    assert settings.tenant_review_base_urls == {
        "johanna": "https://reviews.example.test"
    }


@pytest.mark.parametrize(
    "mapping",
    [
        {"johanna": "http://reviews.example.test"},
        {"johanna": "https://reviews.example.test/path"},
        {"att1": "https://reviews.example.test"},
    ],
)
def test_connector_rejects_unsafe_or_unroutable_review_origins(mapping) -> None:
    with pytest.raises(ValueError, match="invalid_tenant_review_base_urls"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={"johanna": "C0C0YEACVT2"},
                tenant_review_base_urls=mapping,
            )
        )
