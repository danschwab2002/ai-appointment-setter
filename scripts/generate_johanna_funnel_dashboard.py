#!/usr/bin/env python3
"""Generate a sanitary, read-only Johanna funnel dashboard artifact."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sys
from typing import NoReturn
from urllib.parse import urlsplit
import uuid

import httpx

_ALLOWED_CASE_FIELDS = frozenset(
    {
        "case_id",
        "case_type",
        "provenance",
        "stage",
        "commercial_outcome",
        "control_outcomes",
        "created_at",
        "updated_at",
        "conversation_id",
        "chatwoot_conversation_id",
        "chatwoot_status",
        "attention_reasons",
    }
)
_ALLOWED_PROVENANCE = frozenset(
    {"customer_production", "controlled_test", "simulator", "unknown"}
)
_ALLOWED_SOURCE_STATUS = frozenset({"complete", "partial", "unavailable"})
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,127}$")
_TERMINAL_STAGES = frozenset(
    {"completed", "blocked", "failed", "delivery_unknown", "projected", "resolved"}
)


def _die(message: str) -> NoReturn:
    raise ValueError(message)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _die(f"invalid {field}")
    return value.strip()


def _token(value: object, field: str) -> str:
    text = _text(value, field)
    if _TOKEN_RE.fullmatch(text) is None:
        _die(f"invalid {field}")
    return text


def _optional_token(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _token(value, field)


def _token_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        _die(f"invalid {field}")
    return [_token(item, field) for item in value]


def _uuid(value: object, field: str) -> uuid.UUID:
    text = _text(value, field)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    if str(parsed) != text.lower():
        _die(f"invalid {field}")
    return parsed


def _optional_uuid(value: object, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _uuid(value, field)


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    return text


def _utc_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def collect_live_snapshot(
    *,
    client: httpx.Client,
    supabase_base_url: str,
    service_role_key: str,
    cutoff: str,
    window_days: int,
    precheckout_outbound_enabled: bool | None = None,
    chatwoot_app_base_url: str | None = None,
    chatwoot_account_id: int | None = None,
) -> dict[str, object]:
    """Read the closed sanitary projection; never select durable tables directly."""
    if not supabase_base_url.strip() or not service_role_key.strip():
        raise ValueError("missing_supabase_configuration")
    if (
        not isinstance(window_days, int)
        or isinstance(window_days, bool)
        or not 1 <= window_days <= 31
    ):
        raise ValueError("invalid_window_days")
    cutoff_at = _utc_datetime(cutoff, "cutoff")
    response = client.post(
        f"{supabase_base_url.rstrip('/')}/rest/v1/rpc/"
        "read_johanna_funnel_dashboard_v1",
        json={"p_cutoff": cutoff, "p_window_days": window_days},
        headers={
            "apikey": service_role_key,
            "authorization": f"Bearer {service_role_key}",
            "accept": "application/json",
            "content-type": "application/json",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("invalid_supabase_response")

    cases: list[dict[str, object]] = []
    for raw_row in payload:
        row = dict(raw_row)
        controls = row.get("control_outcomes")
        if not isinstance(controls, list):
            raise ValueError("invalid_supabase_response")
        if (
            precheckout_outbound_enabled is False
            and row.get("case_type") in {"precheckout_only", "both"}
            and row.get("stage") == "reserved"
        ):
            controls = [*controls, "outbound_blocked_by_configuration"]
        row["control_outcomes"] = sorted(set(controls))
        cases.append(row)

    window_start = cutoff_at - timedelta(days=window_days)
    snapshot: dict[str, object] = {
        "version": 1,
        "cutoff": _iso_utc(cutoff_at),
        "window_start": _iso_utc(window_start),
        "source_status": {
            "supabase": "complete",
            "chatwoot": (
                "partial"
                if chatwoot_app_base_url is not None and chatwoot_account_id is not None
                else "unavailable"
            ),
        },
        "cases": cases,
    }
    if chatwoot_app_base_url is not None and chatwoot_account_id is not None:
        snapshot["chatwoot_app_base_url"] = chatwoot_app_base_url
        snapshot["chatwoot_account_id"] = chatwoot_account_id
    return snapshot


def sanitize_snapshot(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        _die("unsupported snapshot")
    source_status = raw.get("source_status")
    if not isinstance(source_status, dict):
        _die("invalid source_status")
    statuses: dict[str, str] = {}
    for source in ("supabase", "chatwoot"):
        status = source_status.get(source)
        if status not in _ALLOWED_SOURCE_STATUS:
            _die(f"invalid {source} source status")
        statuses[source] = str(status)

    chatwoot_base = raw.get("chatwoot_app_base_url")
    chatwoot_account = raw.get("chatwoot_account_id")
    if chatwoot_base is not None:
        chatwoot_base = _text(chatwoot_base, "chatwoot_app_base_url").rstrip("/")
        parsed = urlsplit(chatwoot_base)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            _die("invalid chatwoot_app_base_url")
    if chatwoot_account is not None and (
        not isinstance(chatwoot_account, int)
        or isinstance(chatwoot_account, bool)
        or chatwoot_account <= 0
    ):
        _die("invalid chatwoot_account_id")
    if (chatwoot_base is None) != (chatwoot_account is None):
        _die("incomplete chatwoot link configuration")

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list):
        _die("invalid cases")
    cases: list[dict[str, object]] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            _die("invalid case")
        case = {key: raw_case.get(key) for key in _ALLOWED_CASE_FIELDS}
        provenance = _text(case["provenance"], "provenance")
        if provenance not in _ALLOWED_PROVENANCE:
            _die("invalid provenance")
        case_id = _uuid(case["case_id"], "case_id")
        conversation_id = _optional_uuid(case["conversation_id"], "conversation_id")
        chatwoot_id = case["chatwoot_conversation_id"]
        if chatwoot_id is not None and (
            not isinstance(chatwoot_id, int)
            or isinstance(chatwoot_id, bool)
            or chatwoot_id <= 0
        ):
            _die("invalid chatwoot_conversation_id")
        cases.append(
            {
                "safe_id": case_id.hex[:8],
                "case_type": _token(case["case_type"], "case_type"),
                "provenance": provenance,
                "stage": _token(case["stage"], "stage"),
                "commercial_outcome": _token(
                    case["commercial_outcome"], "commercial_outcome"
                ),
                "control_outcomes": _token_list(
                    case["control_outcomes"], "control_outcomes"
                ),
                "created_at": _timestamp(case["created_at"], "created_at"),
                "updated_at": _timestamp(case["updated_at"], "updated_at"),
                "has_conversation": conversation_id is not None
                or chatwoot_id is not None,
                "chatwoot_conversation_id": chatwoot_id,
                "chatwoot_status": _optional_token(
                    case["chatwoot_status"], "chatwoot_status"
                ),
                "attention_reasons": _token_list(
                    case["attention_reasons"], "attention_reasons"
                ),
            }
        )
    return {
        "version": 1,
        "cutoff": _timestamp(raw.get("cutoff"), "cutoff"),
        "window_start": _timestamp(raw.get("window_start"), "window_start"),
        "source_status": statuses,
        "chatwoot_app_base_url": chatwoot_base,
        "chatwoot_account_id": chatwoot_account,
        "cases": cases,
    }


def _card(label: str, value: int, detail: str = "") -> str:
    detail_html = f"<small>{escape(detail)}</small>" if detail else ""
    return (
        '<article class="card">'
        f"<span>{escape(label)}</span><strong>{value}</strong>{detail_html}</article>"
    )


def _funnel(title: str, cases: list[dict[str, object]]) -> str:
    stages = Counter(str(case["stage"]) for case in cases)
    stage_cards = "".join(_card(stage, count) for stage, count in sorted(stages.items()))
    if not stage_cards:
        stage_cards = _card("Casos elegibles", 0)
    return (
        '<article class="funnel">'
        f"<h3>{escape(title)}</h3>"
        f'<p>{len(cases)} casos en la cohorte</p><div class="stage-cards">{stage_cards}</div>'
        "</article>"
    )


def _select_options(values: set[str]) -> str:
    return "".join(
        f'<option value="{escape(value)}">{escape(value)}</option>'
        for value in sorted(values)
    )


def _non_terminal_age_buckets(
    cases: list[dict[str, object]], cutoff: str
) -> Counter[str]:
    cutoff_at = _utc_datetime(cutoff, "cutoff")
    buckets: Counter[str] = Counter()
    for case in cases:
        if (
            case["commercial_outcome"] == "purchased"
            or case["stage"] in _TERMINAL_STAGES
        ):
            continue
        age = max(
            cutoff_at - _utc_datetime(str(case["updated_at"]), "updated_at"),
            timedelta(0),
        )
        if age < timedelta(hours=1):
            buckets["No terminales <1 h"] += 1
        elif age <= timedelta(hours=24):
            buckets["No terminales 1–24 h"] += 1
        else:
            buckets["No terminales >24 h"] += 1
    return buckets


def render_dashboard(snapshot: dict[str, object]) -> str:
    cases = snapshot["cases"]
    assert isinstance(cases, list)
    detail_cases = cases[:100]
    with_conversation = sum(bool(case["has_conversation"]) for case in cases)
    without_conversation = len(cases) - with_conversation
    linked_ids = [
        case["chatwoot_conversation_id"]
        for case in cases
        if case["chatwoot_conversation_id"] is not None
    ]
    linked_counts = Counter(linked_ids)
    shared = sum(count > 1 for count in linked_counts.values())
    attention = sum(bool(case["attention_reasons"]) for case in cases)
    source_status = snapshot["source_status"]
    assert isinstance(source_status, dict)
    chatwoot_base = snapshot.get("chatwoot_app_base_url")
    chatwoot_account = snapshot.get("chatwoot_account_id")
    latest_case = max(
        (str(case["created_at"]) for case in cases), default="sin casos"
    )

    rows: list[str] = []
    for case in detail_cases:
        controls = ", ".join(case["control_outcomes"]) or "none"
        reasons = ", ".join(case["attention_reasons"]) or "—"
        if (
            case["chatwoot_conversation_id"] is not None
            and isinstance(chatwoot_base, str)
            and isinstance(chatwoot_account, int)
        ):
            conversation_id = int(case["chatwoot_conversation_id"])
            url = (
                f"{chatwoot_base}/app/accounts/{chatwoot_account}/"
                f"conversations/{conversation_id}"
            )
            chatwoot = (
                f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
                f"Abrir #{conversation_id}</a>"
            )
        elif case["chatwoot_conversation_id"] is not None:
            chatwoot = "Vínculo disponible; URL no configurada"
        else:
            chatwoot = "Sin vínculo"
        has_attention = "true" if case["attention_reasons"] else "false"
        rows.append(
            f'<tr data-case-type="{escape(str(case["case_type"]))}" '
            f'data-stage="{escape(str(case["stage"]))}" '
            f'data-provenance="{escape(str(case["provenance"]))}" '
            f'data-attention="{has_attention}">'
            f"<td><code>{escape(str(case['safe_id']))}</code></td>"
            f"<td>{escape(str(case['case_type']))}</td>"
            f"<td>{escape(str(case['provenance']))}</td>"
            f"<td>{escape(str(case['stage']))}</td>"
            f"<td>{escape(str(case['commercial_outcome']))}</td>"
            f"<td>{escape(controls)}</td>"
            f"<td>{chatwoot}</td>"
            f"<td>{escape(reasons)}</td>"
            "</tr>"
        )

    cards = "".join(
        (
            _card("Casos de la cohorte", len(cases)),
            _card("Con conversación vinculada", with_conversation),
            _card("Sin conversación", without_conversation),
            _card("Conversaciones vinculadas únicas", len(linked_counts)),
            _card("Compartidas por varios casos", shared),
            _card("Requieren atención", attention),
        )
    )
    inbound_cases = [case for case in cases if case["case_type"] == "inbound"]
    recovery_cases = [
        case
        for case in cases
        if case["case_type"]
        in {"precheckout_only", "hotmart_only", "both"}
    ]
    payment_cases = [
        case for case in cases if case["case_type"] == "payment_failure"
    ]
    funnels = "".join(
        (
            _funnel("Funnel inbound", inbound_cases),
            _funnel("Funnel recuperación", recovery_cases),
            _funnel("Funnel pago fallido", payment_cases),
        )
    )
    health = Counter(
        str(reason) for case in cases for reason in case["attention_reasons"]
    )
    health_cards = "".join(
        _card(reason, count) for reason, count in sorted(health.items())
    ) or _card("Sin razones de atención", 0)
    age_buckets = _non_terminal_age_buckets(cases, str(snapshot["cutoff"]))
    health_cards += "".join(
        _card(label, age_buckets[label])
        for label in (
            "No terminales <1 h",
            "No terminales 1–24 h",
            "No terminales >24 h",
        )
    )
    type_options = _select_options({str(case["case_type"]) for case in detail_cases})
    stage_options = _select_options({str(case["stage"]) for case in detail_cases})
    provenance_options = _select_options(
        {str(case["provenance"]) for case in detail_cases}
    )
    detail_note = f"Mostrando {len(detail_cases)} de {len(cases)} casos"
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Funnel Johanna</title>
<style>
:root {{
  color-scheme: light;
  --page-bg: #f1f5f9;
  --surface: #ffffff;
  --surface-strong: #e2e8f0;
  --text: #0f172a;
  --muted: #475569;
  --border-color: #94a3b8;
  --accent-color: #1d4ed8;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --page-bg: #0b1120;
    --surface: #172033;
    --surface-strong: #1e293b;
    --text: #f8fafc;
    --muted: #cbd5e1;
    --border-color: #64748b;
    --accent-color: #60a5fa;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page-bg); color: var(--text); font: 14px/1.45 inherit, system-ui, sans-serif; }}
main {{ width: 100%; }}
h1, h2 {{ margin: 0 0 8px; }}
p {{ margin: 0 0 16px; color: var(--muted); }}
.status {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0 18px; }}
.badge {{ border: 1px solid var(--border-color); background: var(--surface); border-radius: 999px; padding: 4px 9px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin: 12px 0 24px; }}
.funnels {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin-bottom: 24px; }}
.funnel {{ border: 1px solid var(--border-color); background: var(--surface); border-radius: 10px; padding: 12px; }}
.funnel h3 {{ margin: 0; }}
.stage-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(115px, 1fr)); gap: 7px; }}
.card {{ border: 1px solid var(--border-color); background: var(--surface); border-radius: 10px; padding: 12px; display: grid; gap: 4px; }}
.card span, small {{ color: var(--muted); }}
.card strong {{ font-size: 24px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--border-color); background: var(--surface); border-radius: 10px; }}
table {{ border-collapse: collapse; width: 100%; min-width: 900px; }}
th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border-color); vertical-align: top; }}
th {{ background: var(--surface-strong); color: var(--text); font-weight: 600; }}
code {{ color: var(--accent-color); }}
a {{ color: var(--accent-color); }}
.note {{ border-left: 3px solid var(--accent-color); padding-left: 10px; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
select {{ color: var(--text); background: var(--surface); border: 1px solid var(--border-color); border-radius: 7px; padding: 6px 8px; }}
</style>
</head>
<body><main>
<h1>Funnel Johanna</h1>
<p>Cohorte UTC: {escape(str(snapshot['window_start']))} → {escape(str(snapshot['cutoff']))}</p>
<div class="status">
<span class="badge">Supabase Cloud: {escape(str(source_status['supabase']))}</span>
<span class="badge">Chatwoot: {escape(str(source_status['chatwoot']))}</span>
<span class="badge">Último caso durable: {escape(latest_case)}</span>
<span class="badge">Contenido conversacional: no recopilado</span>
</div>
<section><h2>Cobertura</h2><p class="note">Estas son conversaciones vinculadas a casos; el universo conversacional completo vive en Chatwoot.</p><div class="cards">{cards}</div></section>
<section><h2>Funnels</h2><div class="funnels">{funnels}</div></section>
<section><h2>Salud y atención</h2><div class="cards">{health_cards}</div></section>
<section><h2>Casos</h2><p>{escape(detail_note)}</p>
<div class="filters">
<select id="filter-type"><option value="">Todos los tipos</option>{type_options}</select>
<select id="filter-stage"><option value="">Todas las etapas</option>{stage_options}</select>
<select id="filter-provenance"><option value="">Toda procedencia</option>{provenance_options}</select>
<select id="filter-attention"><option value="">Con y sin atención</option><option value="true">Requiere atención</option><option value="false">Sin atención</option></select>
</div>
<div class="table-wrap"><table><thead><tr><th>Caso</th><th>Tipo</th><th>Procedencia</th><th>Etapa</th><th>Resultado comercial</th><th>Resultados de control</th><th>Chatwoot</th><th>Atención</th></tr></thead><tbody id="case-rows">{''.join(rows) or '<tr><td colspan="8">Sin casos en la cohorte.</td></tr>'}</tbody></table></div></section>
</main>
<script>
(() => {{
  const fields = ["type", "stage", "provenance", "attention"];
  const filters = Object.fromEntries(fields.map(name => [name, document.getElementById(`filter-${{name}}`)]));
  const apply = () => document.querySelectorAll("#case-rows tr[data-case-type]").forEach(row => {{
    row.hidden = fields.some(name => filters[name].value && row.dataset[name === "type" ? "caseType" : name] !== filters[name].value);
  }});
  Object.values(filters).forEach(control => control.addEventListener("change", apply));
}})();
</script>
</body></html>"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path)
    source.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument(
        "--precheckout-outbound-enabled",
        choices=("true", "false"),
        help="Required in live mode; exact deployed final-outbound gate.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.live:
            if args.precheckout_outbound_enabled is None:
                raise ValueError("missing_precheckout_outbound_gate")
            cutoff = args.cutoff or _iso_utc(datetime.now(timezone.utc))
            chatwoot_base = os.getenv("CHATWOOT_BASE_URL", "").strip() or None
            chatwoot_account_text = os.getenv("CHATWOOT_ACCOUNT_ID", "").strip()
            chatwoot_account = int(chatwoot_account_text) if chatwoot_account_text else None
            if (chatwoot_base is None) != (chatwoot_account is None):
                raise ValueError("incomplete_chatwoot_link_configuration")
            if chatwoot_account is not None and chatwoot_account <= 0:
                raise ValueError("invalid_chatwoot_account_id")
            with httpx.Client() as client:
                raw = collect_live_snapshot(
                    client=client,
                    supabase_base_url=os.getenv("SUPABASE_BASE_URL", ""),
                    service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
                    cutoff=cutoff,
                    window_days=args.window_days,
                    precheckout_outbound_enabled=(
                        args.precheckout_outbound_enabled == "true"
                    ),
                    chatwoot_app_base_url=chatwoot_base,
                    chatwoot_account_id=chatwoot_account,
                )
        else:
            assert args.snapshot is not None
            raw = json.loads(args.snapshot.read_text(encoding="utf-8"))
        snapshot = sanitize_snapshot(raw)
        html = render_dashboard(snapshot)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(html, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError):
        print("dashboard_generation_failed", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
