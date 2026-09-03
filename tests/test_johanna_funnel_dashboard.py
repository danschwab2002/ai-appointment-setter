import json
import importlib.util
from pathlib import Path
import subprocess
import sys

import httpx
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_johanna_funnel_dashboard.py"
SPEC = importlib.util.spec_from_file_location("johanna_funnel_dashboard", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_cli_renders_one_sanitized_case_from_snapshot(tmp_path: Path) -> None:
    snapshot = {
        "version": 1,
        "cutoff": "2026-08-31T13:00:00Z",
        "window_start": "2026-08-24T13:00:00Z",
        "source_status": {"supabase": "complete", "chatwoot": "partial"},
        "cases": [
            {
                "case_id": "12345678-1234-5678-9234-567812345678",
                "case_type": "inbound",
                "provenance": "unknown",
                "stage": "case_active",
                "commercial_outcome": "unknown",
                "control_outcomes": ["handoff_pending"],
                "created_at": "2026-08-30T12:00:00Z",
                "updated_at": "2026-08-31T12:00:00Z",
                "conversation_id": "87654321-4321-6789-a321-876543210987",
                "chatwoot_conversation_id": 42,
                "chatwoot_status": "open",
                "attention_reasons": ["provenance_unknown", "handoff_pending"],
                "contact_email": "private@example.test",
            }
        ],
    }
    source = tmp_path / "snapshot.json"
    output = tmp_path / "dashboard.html"
    source.write_text(json.dumps(snapshot), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--snapshot",
            str(source),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(output)
    html = output.read_text(encoding="utf-8")
    assert "Casos de la cohorte" in html
    assert ">1<" in html
    assert "12345678" in html
    assert "12345678-1234-5678-9234-567812345678" not in html
    assert "private@example.test" not in html
    assert "handoff_pending" in html
    assert "conversaciones vinculadas a casos" in html.lower()


def test_renderer_uses_contrasting_surfaces_in_light_and_dark_modes() -> None:
    snapshot = {
        "version": 1,
        "cutoff": "2026-08-31T13:00:00Z",
        "window_start": "2026-08-24T13:00:00Z",
        "source_status": {"supabase": "complete", "chatwoot": "unavailable"},
        "cases": [],
    }

    html = MODULE.render_dashboard(MODULE.sanitize_snapshot(snapshot))

    assert "--page-bg: #f1f5f9" in html
    assert "--surface: #ffffff" in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert "--page-bg: #0b1120" in html
    assert "--surface: #172033" in html
    assert "background: var(--page-bg)" in html
    assert "background: var(--surface)" in html


def test_live_collection_uses_one_sanitary_read_only_rpc() -> None:
    intent_id = "11111111-1111-4111-8111-111111111111"
    rows = [{
        "case_id": intent_id,
        "case_type": "precheckout_only",
        "provenance": "unknown",
        "stage": "reserved",
        "commercial_outcome": "unknown",
        "control_outcomes": [],
        "created_at": "2026-08-30T10:00:00Z",
        "updated_at": "2026-08-31T10:00:00Z",
        "conversation_id": None,
        "chatwoot_conversation_id": None,
        "chatwoot_status": None,
        "attention_reasons": ["provenance_unknown"],
    }]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=rows, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = MODULE.collect_live_snapshot(
            client=client,
            supabase_base_url="https://supabase.invalid",
            service_role_key="secret-not-for-output",
            cutoff="2026-08-31T13:00:00Z",
            window_days=7,
            precheckout_outbound_enabled=False,
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/v1/rpc/read_johanna_funnel_dashboard_v1"
    assert json.loads(request.content) == {
        "p_cutoff": "2026-08-31T13:00:00Z",
        "p_window_days": 7,
    }
    assert request.headers["authorization"] == "Bearer secret-not-for-output"
    for forbidden in (
        b"raw_payload", b"canonical_payload", b"normalized_email",
        b"normalized_phone", b"target_phone",
    ):
        assert forbidden not in request.content
    assert snapshot["source_status"] == {
        "supabase": "complete",
        "chatwoot": "unavailable",
    }
    assert snapshot["cases"] == [
        {
            "case_id": intent_id,
            "case_type": "precheckout_only",
            "provenance": "unknown",
            "stage": "reserved",
            "commercial_outcome": "unknown",
            "control_outcomes": ["outbound_blocked_by_configuration"],
            "created_at": "2026-08-30T10:00:00Z",
            "updated_at": "2026-08-31T10:00:00Z",
            "conversation_id": None,
            "chatwoot_conversation_id": None,
            "chatwoot_status": None,
            "attention_reasons": ["provenance_unknown"],
        }
    ]


def test_live_cli_uses_environment_without_printing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "dashboard.html"
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://supabase.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret-must-stay-hidden")
    monkeypatch.setenv("CHATWOOT_BASE_URL", "https://chatwoot.example")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "7")
    observed: dict[str, object] = {}

    def collect(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "version": 1,
            "cutoff": "2026-08-31T13:00:00Z",
            "window_start": "2026-08-24T13:00:00Z",
            "source_status": {
                "supabase": "complete",
                "chatwoot": "unavailable",
            },
            "cases": [],
        }

    monkeypatch.setattr(MODULE, "collect_live_snapshot", collect)

    result = MODULE.main(
        [
            "--live",
            "--cutoff",
            "2026-08-31T13:00:00Z",
            "--window-days",
            "7",
            "--precheckout-outbound-enabled",
            "false",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.strip() == str(output)
    assert captured.err == ""
    assert "secret-must-stay-hidden" not in captured.out
    assert observed["service_role_key"] == "secret-must-stay-hidden"
    assert observed["precheckout_outbound_enabled"] is False
    assert observed["chatwoot_app_base_url"] == "https://chatwoot.example"
    assert observed["chatwoot_account_id"] == 7
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_renderer_has_funnels_health_filters_and_chatwoot_links() -> None:
    raw = {
        "version": 1,
        "cutoff": "2026-08-31T13:00:00Z",
        "window_start": "2026-08-24T13:00:00Z",
        "source_status": {"supabase": "complete", "chatwoot": "partial"},
        "chatwoot_app_base_url": "https://chatwoot.example",
        "chatwoot_account_id": 7,
        "cases": [
            {
                "case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "case_type": "inbound",
                "provenance": "unknown",
                "stage": "active",
                "commercial_outcome": "unknown",
                "control_outcomes": [],
                "created_at": "2026-08-30T12:00:00Z",
                "updated_at": "2026-08-31T12:00:00Z",
                "conversation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "chatwoot_conversation_id": 42,
                "chatwoot_status": None,
                "attention_reasons": ["provenance_unknown"],
            },
            {
                "case_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "case_type": "precheckout_only",
                "provenance": "controlled_test",
                "stage": "reserved",
                "commercial_outcome": "unknown",
                "control_outcomes": ["outbound_blocked_by_configuration"],
                "created_at": "2026-08-30T11:00:00Z",
                "updated_at": "2026-08-31T11:00:00Z",
                "conversation_id": None,
                "chatwoot_conversation_id": None,
                "chatwoot_status": None,
                "attention_reasons": [],
            },
            {
                "case_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "case_type": "payment_failure",
                "provenance": "unknown",
                "stage": "delivery_unknown",
                "commercial_outcome": "unknown",
                "control_outcomes": ["delivery_unknown"],
                "created_at": "2026-08-30T10:00:00Z",
                "updated_at": "2026-08-31T10:00:00Z",
                "conversation_id": None,
                "chatwoot_conversation_id": 84,
                "chatwoot_status": None,
                "attention_reasons": ["delivery_unknown", "provenance_unknown"],
            },
        ],
    }

    html = MODULE.render_dashboard(MODULE.sanitize_snapshot(raw))

    assert "Funnel inbound" in html
    assert "Funnel recuperación" in html
    assert "Funnel pago fallido" in html
    assert "Salud y atención" in html
    assert 'id="filter-type"' in html
    assert 'id="filter-stage"' in html
    assert 'id="filter-provenance"' in html
    assert 'id="filter-attention"' in html
    assert 'data-case-type="inbound"' in html
    assert (
        'href="https://chatwoot.example/app/accounts/7/conversations/42"' in html
    )
    assert "Contenido conversacional: no recopilado" in html
    assert "Último caso durable: 2026-08-30T12:00:00Z" in html


def test_cli_rejects_pii_inside_allowed_reason_fields_without_echo(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe.json"
    output = tmp_path / "dashboard.html"
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "cutoff": "2026-08-31T13:00:00Z",
                "window_start": "2026-08-24T13:00:00Z",
                "source_status": {
                    "supabase": "complete",
                    "chatwoot": "unavailable",
                },
                "cases": [
                    {
                        "case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "case_type": "inbound",
                        "provenance": "unknown",
                        "stage": "active",
                        "commercial_outcome": "unknown",
                        "control_outcomes": [],
                        "created_at": "2026-08-30T12:00:00Z",
                        "updated_at": "2026-08-31T12:00:00Z",
                        "conversation_id": None,
                        "chatwoot_conversation_id": None,
                        "chatwoot_status": None,
                        "attention_reasons": ["private@example.test"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--snapshot",
            str(source),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "dashboard_generation_failed"
    assert "private@example.test" not in result.stdout + result.stderr
    assert not output.exists()


def test_renderer_keeps_complete_counts_but_caps_case_detail_at_100() -> None:
    cases = []
    for number in range(101):
        cases.append(
            {
                "case_id": f"00000000-0000-4000-8000-{number:012d}",
                "case_type": "inbound",
                "provenance": "unknown",
                "stage": "active",
                "commercial_outcome": "unknown",
                "control_outcomes": [],
                "created_at": "2026-08-30T12:00:00Z",
                "updated_at": "2026-08-31T12:00:00Z",
                "conversation_id": None,
                "chatwoot_conversation_id": None,
                "chatwoot_status": None,
                "attention_reasons": ["provenance_unknown"],
            }
        )
    raw = {
        "version": 1,
        "cutoff": "2026-08-31T13:00:00Z",
        "window_start": "2026-08-24T13:00:00Z",
        "source_status": {"supabase": "complete", "chatwoot": "unavailable"},
        "cases": cases,
    }

    html = MODULE.render_dashboard(MODULE.sanitize_snapshot(raw))

    assert "Casos de la cohorte</span><strong>101</strong>" in html
    assert html.count("<tr data-case-type=") == 100
    assert "Mostrando 100 de 101 casos" in html


def test_renderer_groups_non_terminal_cases_by_age_at_cutoff() -> None:
    cases = []
    for number, updated_at in enumerate(
        (
            "2026-08-31T12:30:00Z",
            "2026-08-31T03:00:00Z",
            "2026-08-29T12:00:00Z",
        )
    ):
        cases.append(
            {
                "case_id": f"10000000-0000-4000-8000-{number:012d}",
                "case_type": "inbound",
                "provenance": "unknown",
                "stage": "active",
                "commercial_outcome": "unknown",
                "control_outcomes": [],
                "created_at": updated_at,
                "updated_at": updated_at,
                "conversation_id": None,
                "chatwoot_conversation_id": None,
                "chatwoot_status": None,
                "attention_reasons": ["provenance_unknown"],
            }
        )
    raw = {
        "version": 1,
        "cutoff": "2026-08-31T13:00:00Z",
        "window_start": "2026-08-24T13:00:00Z",
        "source_status": {"supabase": "complete", "chatwoot": "unavailable"},
        "cases": cases,
    }

    html = MODULE.render_dashboard(MODULE.sanitize_snapshot(raw))

    assert "No terminales &lt;1 h</span><strong>1</strong>" in html
    assert "No terminales 1–24 h</span><strong>1</strong>" in html
    assert "No terminales &gt;24 h</span><strong>1</strong>" in html
