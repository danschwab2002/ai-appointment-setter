"""Collect, minimize, and render private daily conversation review packages."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Sequence

import httpx

from .daily_feedback import (
    CreateBatchResult,
    DailyFeedbackBatchStore,
    MinimizedReviewConversation,
    RealConversationBatchGrant,
    ReviewTranscriptMessage,
    sanitize_review_text,
)


class ConversationCollectionError(RuntimeError):
    """The source could not prove a complete, correctly scoped daily collection."""


@dataclass(frozen=True)
class RealConversationSecurityPolicy:
    real_collection_enabled: bool
    storage_encryption_verified: bool
    retention_hours: int
    deletion_owner: str


@dataclass(frozen=True)
class ReviewMessage:
    message_ref: str
    actor: str
    text: str
    occurred_at: datetime


@dataclass(frozen=True)
class ReviewConversation:
    conversation_ref: str
    display_label: str
    messages: tuple[ReviewMessage, ...]
    apparent_objective: str
    observed_outcome: str
    release_id: str
    release_version: int


@dataclass(frozen=True)
class DailyReviewPackage:
    schema_version: str
    tenant_ref: str
    scope_ref: str
    window_start: datetime
    window_end: datetime
    conversations: tuple[ReviewConversation, ...]
    sanitizer_version: str = "deterministic-redaction-v1"
    selection_version: str = "chatwoot-daily-agent-dialogues-v1"
    retention_hours: int | None = None
    deletion_owner: str | None = None
    storage_encryption_verified: bool = False


@dataclass(frozen=True)
class ReviewBundleResult:
    html_path: Path
    manifest_path: Path


def materialize_daily_review_package(
    *,
    store: DailyFeedbackBatchStore,
    command_id: str,
    package: DailyReviewPackage,
    authority: RealConversationBatchGrant,
) -> CreateBatchResult:
    """Commit one minimized package to the authoritative durable review workflow."""
    if (
        package.schema_version != "daily-feedback-review-package-v1"
        or package.retention_hours is None
        or package.deletion_owner is None
        or package.storage_encryption_verified is not True
        or authority.active is not True
        or authority.tenant_id != package.tenant_ref
        or authority.scope_id != package.scope_ref
        or authority.package_schema_version != package.schema_version
        or authority.sanitizer_version != package.sanitizer_version
        or authority.selection_version != package.selection_version
        or authority.retention_hours != package.retention_hours
        or authority.deletion_owner != package.deletion_owner
        or not authority.storage_encryption_evidence_ref.strip()
    ):
        raise ConversationCollectionError("review_package_not_materializable")
    if any(
        sanitize_message_text(message.text, names=()) != message.text
        for conversation in package.conversations
        for message in conversation.messages
    ):
        raise ConversationCollectionError("review_package_sanitization_mismatch")
    if any(
        sanitize_message_text(value, names=()) != value
        for conversation in package.conversations
        for value in (
            conversation.display_label,
            conversation.apparent_objective,
            conversation.observed_outcome,
        )
    ):
        raise ConversationCollectionError("review_package_sanitization_mismatch")
    if any(
        conversation.release_id != "release_lineage_unavailable"
        or conversation.release_version != 0
        for conversation in package.conversations
    ):
        raise ConversationCollectionError(
            "review_package_release_lineage_unavailable_required"
        )
    return store.create_minimized_review_batch(
        command_id=command_id,
        tenant_id=package.tenant_ref,
        scope_id=package.scope_ref,
        window_start=package.window_start,
        window_end=package.window_end,
        sanitizer_version=package.sanitizer_version,
        selection_version=package.selection_version,
        authority=authority,
        conversations=tuple(
            MinimizedReviewConversation(
                conversation_ref=conversation.conversation_ref,
                context_summary=conversation.display_label,
                apparent_objective=conversation.apparent_objective,
                observed_outcome=conversation.observed_outcome,
                release_id=conversation.release_id,
                release_version=conversation.release_version,
                messages=tuple(
                    ReviewTranscriptMessage(
                        message_ref=message.message_ref,
                        actor=message.actor,
                        text=message.text,
                        occurred_at=message.occurred_at,
                    )
                    for message in conversation.messages
                ),
            )
            for conversation in package.conversations
        ),
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _hmac_ref(key: bytes, namespace: str, value: object) -> str:
    digest = hmac.new(
        key,
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]
    return f"{namespace}_{digest}"


sanitize_message_text = sanitize_review_text


class ChatwootDailyCollector:
    """Read one configured Chatwoot inbox into a local minimized package."""

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        inbox_id: int,
        agent_bot_id: int,
        access_token: str,
        pseudonymization_key: bytes,
        security_policy: RealConversationSecurityPolicy,
        transport: httpx.BaseTransport | None = None,
        max_conversation_pages: int = 20,
        max_message_pages: int = 20,
    ) -> None:
        origin = httpx.URL(base_url)
        if (
            origin.scheme != "https"
            or not origin.host
            or bool(origin.username)
            or bool(origin.password)
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ConversationCollectionError("chatwoot_https_origin_required")
        if any(type(value) is not int or value <= 0 for value in (account_id, inbox_id, agent_bot_id)):
            raise ValueError("chatwoot_ids_must_be_positive_integers")
        if not access_token:
            raise ValueError("chatwoot_access_token_required")
        if len(pseudonymization_key) < 32:
            raise ValueError("pseudonymization_key_too_short")
        if (
            type(security_policy.real_collection_enabled) is not bool
            or type(security_policy.storage_encryption_verified) is not bool
            or security_policy.real_collection_enabled is not True
            or security_policy.storage_encryption_verified is not True
            or type(security_policy.retention_hours) is not int
            or not 1 <= security_policy.retention_hours <= 168
            or not security_policy.deletion_owner.strip()
        ):
            raise ConversationCollectionError(
                "real_conversation_collection_not_authorized"
            )
        if max_conversation_pages < 1 or max_message_pages < 1:
            raise ValueError("page_limits_must_be_positive")
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._agent_bot_id = agent_bot_id
        self._access_token = access_token
        self._key = pseudonymization_key
        self._security_policy = security_policy
        self._transport = transport
        self._max_conversation_pages = max_conversation_pages
        self._max_message_pages = max_message_pages

    def collect(
        self,
        *,
        tenant_ref: str,
        scope_ref: str,
        window_start: datetime,
        window_end: datetime,
    ) -> DailyReviewPackage:
        start = _utc(window_start, "window_start")
        end = _utc(window_end, "window_end")
        if start >= end:
            raise ValueError("invalid_review_window")
        if not tenant_ref or not scope_ref:
            raise ValueError("invalid_review_package_identity")

        with httpx.Client(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=20,
        ) as client:
            raw_conversations = self._list_conversations(client)
            collected: list[tuple[datetime, int, tuple[ReviewMessage, ...], str]] = []
            for conversation in raw_conversations:
                conversation_id = conversation.get("id")
                inbox_id = conversation.get("inbox_id")
                updated_at = conversation.get("updated_at")
                if (
                    type(conversation_id) is not int
                    or conversation_id <= 0
                    or type(inbox_id) is not int
                    or inbox_id != self._inbox_id
                    or type(updated_at) is not int
                    or updated_at <= 0
                ):
                    raise ConversationCollectionError("chatwoot_conversation_scope_mismatch")
                if datetime.fromtimestamp(updated_at, tz=UTC) < start:
                    continue
                names = self._conversation_names(conversation)
                messages = self._collect_messages(
                    client,
                    conversation_id=conversation_id,
                    window_start=start,
                    window_end=end,
                    names=names,
                )
                actors = {message.actor for message in messages}
                if not messages or not {"prospect", "agent"}.issubset(actors):
                    continue
                collected.append((messages[0].occurred_at, conversation_id, messages, names[0] if names else ""))

        collected.sort(key=lambda item: (item[0], item[1]))
        conversations = tuple(
            ReviewConversation(
                conversation_ref=_hmac_ref(
                    self._key,
                    "conv",
                    f"{tenant_ref}:{scope_ref}:{conversation_id}",
                ),
                display_label=f"Conversación {position:02d}",
                messages=messages,
                apparent_objective="Revisar la respuesta del agente",
                observed_outcome=(
                    "Esperando respuesta del prospecto"
                    if messages[-1].actor == "agent"
                    else "El prospecto continuó la conversación"
                ),
                release_id="release_lineage_unavailable",
                release_version=0,
            )
            for position, (_, conversation_id, messages, _) in enumerate(collected, start=1)
        )
        return DailyReviewPackage(
            schema_version="daily-feedback-review-package-v1",
            tenant_ref=tenant_ref,
            scope_ref=scope_ref,
            window_start=start,
            window_end=end,
            conversations=conversations,
            retention_hours=self._security_policy.retention_hours,
            deletion_owner=self._security_policy.deletion_owner,
            storage_encryption_verified=(
                self._security_policy.storage_encryption_verified
            ),
        )

    def _list_conversations(self, client: httpx.Client) -> list[dict[str, object]]:
        path = f"/api/v1/accounts/{self._account_id}/conversations"
        result: list[dict[str, object]] = []
        seen_conversation_ids: set[int] = set()
        expected_count: int | None = None
        for page_number in range(1, self._max_conversation_pages + 1):
            response = client.get(
                path,
                params={"inbox_id": self._inbox_id, "status": "all", "page": page_number},
            )
            response.raise_for_status()
            body = _json_object(response, "invalid_chatwoot_conversation_list")
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            payload = data["payload"]
            if not all(isinstance(item, dict) for item in payload):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            for item in payload:
                conversation_id = item.get("id")
                if type(conversation_id) is not int or conversation_id <= 0:
                    raise ConversationCollectionError(
                        "invalid_chatwoot_conversation_list"
                    )
                if conversation_id in seen_conversation_ids:
                    raise ConversationCollectionError(
                        "chatwoot_conversation_pagination_repeated"
                    )
                seen_conversation_ids.add(conversation_id)
            result.extend(payload)
            meta = data.get("meta")
            if not isinstance(meta, dict):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            current = meta.get("current_page")
            all_count = meta.get("all_count")
            if (
                type(current) is not int
                or current != page_number
                or type(all_count) is not int
                or all_count < 0
                or len(result) > all_count
            ):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            if expected_count is None:
                expected_count = all_count
            elif all_count != expected_count:
                raise ConversationCollectionError(
                    "chatwoot_conversation_count_changed"
                )
            if len(result) == expected_count:
                return result
            if not payload:
                raise ConversationCollectionError(
                    "chatwoot_conversation_list_incomplete"
                )
        raise ConversationCollectionError("chatwoot_conversation_page_limit_reached")

    def _collect_messages(
        self,
        client: httpx.Client,
        *,
        conversation_id: int,
        window_start: datetime,
        window_end: datetime,
        names: tuple[str, ...],
    ) -> tuple[ReviewMessage, ...]:
        path = f"/api/v1/accounts/{self._account_id}/conversations/{conversation_id}/messages"
        before: int | None = None
        by_id: dict[int, dict[str, object]] = {}
        boundary_proven = False
        for _ in range(self._max_message_pages):
            response = client.get(path, params={"before": before} if before is not None else None)
            response.raise_for_status()
            body = _json_object(response, "invalid_chatwoot_message_list")
            payload = body.get("payload")
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise ConversationCollectionError("invalid_chatwoot_message_list")
            if not payload:
                boundary_proven = True
                break
            page_ids: list[int] = []
            page_times: list[int] = []
            for message in payload:
                message_id = message.get("id")
                created_at = message.get("created_at")
                if type(message_id) is not int or message_id <= 0 or type(created_at) is not int or created_at <= 0:
                    raise ConversationCollectionError("invalid_chatwoot_message")
                page_ids.append(message_id)
                page_times.append(created_at)
                by_id[message_id] = message
            if min(page_times) <= int(window_start.timestamp()):
                boundary_proven = True
                break
            next_before = min(page_ids)
            if before == next_before:
                raise ConversationCollectionError("chatwoot_message_history_did_not_advance")
            before = next_before
        if not boundary_proven:
            raise ConversationCollectionError("chatwoot_message_page_limit_reached")

        selected: list[ReviewMessage] = []
        for message_id, message in sorted(
            by_id.items(), key=lambda item: (int(item[1]["created_at"]), item[0])
        ):
            occurred_at = datetime.fromtimestamp(int(message["created_at"]), tz=UTC)
            if not (window_start <= occurred_at < window_end):
                continue
            actor = self._message_actor(message)
            if actor is None:
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            selected.append(
                ReviewMessage(
                    message_ref=_hmac_ref(
                        self._key,
                        "msg",
                        f"{conversation_id}:{message_id}",
                    ),
                    actor=actor,
                    text=sanitize_message_text(content, names=names),
                    occurred_at=occurred_at,
                )
            )
        return tuple(selected)

    def _message_actor(self, message: dict[str, object]) -> str | None:
        if message.get("private") is not False:
            return None
        sender = message.get("sender")
        if not isinstance(sender, dict):
            return None
        message_type = message.get("message_type")
        if message_type == 0 and sender.get("type") == "contact":
            return "prospect"
        if (
            message_type == 1
            and sender.get("type") == "agent_bot"
            and sender.get("id") == self._agent_bot_id
            and message.get("status") in {"sent", "delivered", "read"}
        ):
            return "agent"
        return None

    @staticmethod
    def _conversation_names(conversation: dict[str, object]) -> tuple[str, ...]:
        meta = conversation.get("meta")
        if not isinstance(meta, dict):
            return ()
        sender = meta.get("sender")
        if not isinstance(sender, dict):
            return ()
        name = sender.get("name")
        return (name,) if isinstance(name, str) and name.strip() else ()


def _json_object(response: httpx.Response, reason: str) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ConversationCollectionError(reason) from exc
    if not isinstance(body, dict):
        raise ConversationCollectionError(reason)
    return body


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _package_fingerprint(package: DailyReviewPackage) -> str:
    payload = {
        "schema_version": package.schema_version,
        "tenant_ref": package.tenant_ref,
        "scope_ref": package.scope_ref,
        "window_start": _iso(package.window_start),
        "window_end": _iso(package.window_end),
        "sanitizer_version": package.sanitizer_version,
        "selection_version": package.selection_version,
        "retention_hours": package.retention_hours,
        "deletion_owner": package.deletion_owner,
        "storage_encryption_verified": package.storage_encryption_verified,
        "conversations": [
            {
                "conversation_ref": conversation.conversation_ref,
                "release_id": conversation.release_id,
                "release_version": conversation.release_version,
                "messages": [
                    {
                        "message_ref": message.message_ref,
                        "actor": message.actor,
                        "text": message.text,
                        "occurred_at": _iso(message.occurred_at),
                    }
                    for message in conversation.messages
                ],
            }
            for conversation in package.conversations
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_review_html(package: DailyReviewPackage) -> str:
    """Render a self-contained, network-inert local inspection surface."""
    total = len(package.conversations)
    badge = "NO DISTRIBUIR · SUPERFICIE LOCAL"
    nav = []
    panels = []
    for index, conversation in enumerate(package.conversations, start=1):
        nav.append(
            f'<button class="conversation-nav{" active" if index == 1 else ""}" data-index="{index - 1}">'
            f'<span>{html.escape(conversation.display_label)}</span><small>Pendiente</small></button>'
        )
        messages = []
        for message in conversation.messages:
            actor_label = "Prospecto" if message.actor == "prospect" else "Agente"
            messages.append(
                f'<article class="message {message.actor}"><header>{actor_label}<time>{html.escape(_iso(message.occurred_at)[11:16])} UTC</time></header>'
                f'<p>{html.escape(message.text)}</p></article>'
            )
        panels.append(
            f'<section class="conversation-panel{" active" if index == 1 else ""}" data-panel="{index - 1}" data-ref="{html.escape(conversation.conversation_ref)}">'
            f'<div class="panel-head"><div><p class="eyebrow">{index} de {total}</p><h2>{html.escape(conversation.display_label)}</h2></div>'
            f'<div class="release">Release {html.escape(conversation.release_id)} · v{conversation.release_version}</div></div>'
            f'<div class="summary"><span><b>Objetivo aparente</b>{html.escape(conversation.apparent_objective)}</span>'
            f'<span><b>Resultado observado</b>{html.escape(conversation.observed_outcome)}</span></div>'
            f'<div class="transcript">{"".join(messages)}</div>'
            '<fieldset><legend>Evaluación</legend><div class="choices">'
            f'<label><input type="radio" name="decision-{index}" value="correct"> Correcta</label>'
            f'<label><input type="radio" name="decision-{index}" value="correct_with_feedback"> Correcta con feedback</label>'
            f'<label><input type="radio" name="decision-{index}" value="skip"> Omitir</label></div>'
            f'<label class="feedback">Feedback literal<textarea data-feedback="{index - 1}" placeholder="Escribí qué debería corregirse. No se aplicará automáticamente."></textarea></label></fieldset>'
            '</section>'
        )
    empty = '<div class="empty"><h2>No hay conversaciones elegibles</h2><p>El lote se generó correctamente, sin elementos para revisar.</p></div>' if not panels else ""
    package_ref = _package_fingerprint(package)
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
<title>Revisión diaria de conversaciones</title><style>
:root{{--ink:#19201c;--muted:#68716b;--paper:#f4f2eb;--surface:#fffdfa;--line:#d9d8d0;--accent:#22644a;--prospect:#e8eee9;--agent:#f3ead7;--warning:#9b4d1f}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 ui-sans-serif,"Segoe UI",sans-serif}}button,input,textarea{{font:inherit}}.shell{{display:grid;grid-template-columns:290px minmax(0,1fr);min-height:100vh}}aside{{padding:24px 18px;border-right:1px solid var(--line);background:#eceae2;position:sticky;top:0;height:100vh;overflow:auto}}.brand{{font:700 18px/1.2 Georgia,serif;margin:0 0 6px}}.window{{color:var(--muted);font-size:12px;margin-bottom:18px}}.badge{{display:block;color:var(--warning);font-size:11px;font-weight:800;letter-spacing:.06em;margin:12px 0 20px}}.conversation-nav{{width:100%;display:flex;justify-content:space-between;align-items:center;border:0;border-top:1px solid var(--line);padding:14px 8px;background:transparent;text-align:left;cursor:pointer;color:var(--ink)}}.conversation-nav:last-child{{border-bottom:1px solid var(--line)}}.conversation-nav small{{color:var(--muted)}}.conversation-nav.active{{color:var(--accent);font-weight:750}}main{{max-width:920px;width:100%;padding:36px 48px 56px}}.conversation-panel{{display:none}}.conversation-panel.active{{display:block}}.panel-head{{display:flex;gap:24px;justify-content:space-between;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:16px}}.eyebrow{{margin:0 0 3px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em}}h2{{font:700 32px/1.1 Georgia,serif;margin:0}}.release{{color:var(--muted);font-size:12px;text-align:right}}.summary{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);margin:18px 0 28px}}.summary span{{background:var(--surface);padding:13px 15px}}.summary b{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:4px}}.transcript{{display:flex;flex-direction:column;gap:10px;margin-bottom:30px}}.message{{max-width:78%;padding:13px 15px;border-radius:4px}}.message.prospect{{background:var(--prospect);align-self:flex-start}}.message.agent{{background:var(--agent);align-self:flex-end}}.message header{{display:flex;justify-content:space-between;gap:18px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}}.message time{{font-weight:500;color:var(--muted)}}.message p{{margin:6px 0 0;white-space:pre-wrap}}fieldset{{border:1px solid var(--line);padding:18px;background:var(--surface)}}legend{{font-weight:800;padding:0 7px}}.choices{{display:flex;flex-wrap:wrap;gap:16px}}.choices label{{min-height:44px;display:flex;align-items:center;gap:7px}}.feedback{{display:block;margin-top:14px;font-weight:700}}textarea{{display:block;width:100%;min-height:96px;margin-top:7px;padding:10px;border:1px solid var(--line);background:white;resize:vertical}}.actions{{display:flex;gap:10px;margin-top:18px}}.actions button{{min-height:44px;border:1px solid var(--ink);padding:0 16px;background:var(--ink);color:white;cursor:pointer}}.actions button.secondary{{background:transparent;color:var(--ink)}}.footnote{{color:var(--muted);font-size:12px;margin-top:18px}}.empty{{padding:50px 0}}@media(max-width:760px){{.shell{{display:block}}aside{{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}}main{{padding:26px 18px}}.summary{{grid-template-columns:1fr}}.message{{max-width:92%}}}}
</style></head><body><div class="shell"><aside><p class="brand">Revisión diaria</p><div class="window">{html.escape(_iso(package.window_start)[:10])} · {total} conversaciones</div><span class="badge">{html.escape(badge)}</span><nav>{''.join(nav)}</nav></aside><main>{''.join(panels)}{empty}<div class="actions"><button id="previous" class="secondary">Anterior</button><button id="next">Siguiente</button><button id="export" class="secondary">Exportar decisiones</button></div><p class="footnote">El feedback se exporta como candidato de revisión. Cambios activados: 0.</p></main></div>
<script>(()=>{{'use strict';const panels=[...document.querySelectorAll('.conversation-panel')],nav=[...document.querySelectorAll('.conversation-nav')];let current=0;function show(i){{if(!panels.length)return;current=Math.max(0,Math.min(i,panels.length-1));panels.forEach((p,n)=>p.classList.toggle('active',n===current));nav.forEach((b,n)=>b.classList.toggle('active',n===current));}}nav.forEach((b,i)=>b.addEventListener('click',()=>show(i)));document.getElementById('previous').addEventListener('click',()=>show(current-1));document.getElementById('next').addEventListener('click',()=>show(current+1));document.getElementById('export').addEventListener('click',()=>{{const decisions=panels.map((p,i)=>({{conversation_ref:p.dataset.ref,decision:(document.querySelector(`input[name="decision-${{i+1}}"]:checked`)||{{}}).value||null,verbatim_feedback:(p.querySelector('textarea').value||'').trim()||null}}));const payload={{schema_version:'daily-feedback-owner-decisions-v1',package_ref:'{package_ref}',activated_changes:0,decisions}};const blob=new Blob([JSON.stringify(payload,null,2)],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='decisiones-feedback.json';a.click();URL.revokeObjectURL(a.href);}});show(0);}})();</script></body></html>'''


def write_private_review_bundle(
    *,
    output_dir: Path,
    package: DailyReviewPackage,
    now: datetime | None = None,
    _after_html_write: Callable[[], None] | None = None,
) -> ReviewBundleResult:
    """Atomically publish a new private directory with HTML and manifest."""
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(output_dir):
        target_stat = output_dir.lstat()
        if stat.S_ISDIR(target_stat.st_mode) and any(output_dir.iterdir()):
            raise ConversationCollectionError("review_output_directory_not_empty")
        raise ConversationCollectionError("review_output_already_exists")

    current = _utc(now or datetime.now(UTC), "now")
    policy_ready = (
        package.storage_encryption_verified is True
        and type(package.retention_hours) is int
        and 1 <= package.retention_hours <= 168
        and isinstance(package.deletion_owner, str)
        and bool(package.deletion_owner.strip())
    )
    distribution_ready = False
    review_html = render_review_html(package)
    html_bytes = review_html.encode("utf-8")
    manifest = {
        "schema_version": package.schema_version,
        "package_ref": _package_fingerprint(package),
        "tenant_ref": package.tenant_ref,
        "scope_ref": package.scope_ref,
        "window_start": _iso(package.window_start),
        "window_end": _iso(package.window_end),
        "created_at": _iso(current),
        "expires_at": (
            _iso(current + timedelta(hours=package.retention_hours))
            if policy_ready and package.retention_hours is not None
            else None
        ),
        "conversation_count": len(package.conversations),
        "sanitizer_version": package.sanitizer_version,
        "selection_version": package.selection_version,
        "retention_hours": package.retention_hours,
        "storage_encryption_verified": package.storage_encryption_verified,
        "deletion_owner_present": bool(package.deletion_owner),
        "human_reviewed": False,
        "distribution_ready": distribution_ready,
        "activated_changes": 0,
        "html_sha256": "sha256:" + hashlib.sha256(html_bytes).hexdigest(),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent)
    )
    os.chmod(staging_dir, 0o700)
    try:
        staging_html = staging_dir / "review.html"
        staging_manifest = staging_dir / "manifest.json"
        _atomic_private_write(staging_html, html_bytes)
        if _after_html_write is not None:
            _after_html_write()
        _atomic_private_write(staging_manifest, manifest_bytes)
        staging_fd = os.open(
            staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        os.rename(staging_dir, output_dir)
        parent_fd = os.open(
            output_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        if staging_dir.exists():
            for child in staging_dir.iterdir():
                child_stat = child.lstat()
                if stat.S_ISREG(child_stat.st_mode) and child.name in {
                    "review.html",
                    "manifest.json",
                }:
                    child.unlink()
            staging_dir.rmdir()
        raise
    return ReviewBundleResult(
        html_path=output_dir / "review.html",
        manifest_path=output_dir / "manifest.json",
    )


def approve_private_review_bundle(
    *,
    bundle_dir: Path,
    expected_html_sha256: str,
    reviewer_binding_ref: str,
    now: datetime,
) -> dict[str, object]:
    """Approve the exact existing HTML bytes without recollecting or rerendering."""
    current = _utc(now, "now")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_html_sha256) is None:
        raise ConversationCollectionError("invalid_expected_html_hash")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", reviewer_binding_ref) is None:
        raise ConversationCollectionError("invalid_reviewer_binding_ref")

    directory_fd = os.open(
        bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ConversationCollectionError("review_bundle_not_private")
        if sorted(os.listdir(directory_fd)) != ["manifest.json", "review.html"]:
            raise ConversationCollectionError("review_bundle_inventory_mismatch")
        manifest_bytes = _read_private_bundle_file(directory_fd, "manifest.json")
        html_bytes = _read_private_bundle_file(directory_fd, "review.html")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if not isinstance(manifest, dict):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        actual_html_hash = "sha256:" + hashlib.sha256(html_bytes).hexdigest()
        if (
            manifest.get("html_sha256") != actual_html_hash
            or expected_html_sha256 != actual_html_hash
        ):
            raise ConversationCollectionError("review_bundle_integrity_error")
        expires_at = manifest.get("expires_at")
        if not isinstance(expires_at, str):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        try:
            expiry = _utc(
                datetime.fromisoformat(expires_at.replace("Z", "+00:00")),
                "expires_at",
            )
        except ValueError as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if current >= expiry:
            raise ConversationCollectionError("review_bundle_expired")
        policy_ready = (
            manifest.get("storage_encryption_verified") is True
            and type(manifest.get("retention_hours")) is int
            and 1 <= manifest["retention_hours"] <= 168
            and manifest.get("deletion_owner_present") is True
        )
        if not policy_ready:
            raise ConversationCollectionError("review_bundle_policy_not_ready")
        if manifest.get("human_reviewed") is True:
            if (
                manifest.get("distribution_ready") is False
                and manifest.get("reviewed_html_sha256") == actual_html_hash
                and manifest.get("reviewer_binding_ref") == reviewer_binding_ref
            ):
                return manifest
            raise ConversationCollectionError("review_bundle_approval_conflict")

        approved = dict(manifest)
        approved.update(
            {
                "human_reviewed": True,
                "distribution_ready": False,
                "distribution_blocked_reason": (
                    "authorized_batch_api_not_implemented"
                ),
                "reviewed_html_sha256": actual_html_hash,
                "reviewer_binding_ref": reviewer_binding_ref,
                "reviewed_at": _iso(current),
            }
        )
        approved_bytes = (
            json.dumps(approved, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_private_write(bundle_dir / "manifest.json", approved_bytes)
        return approved
    finally:
        os.close(directory_fd)


def purge_expired_review_bundle(*, bundle_dir: Path, now: datetime) -> bool:
    """Delete exactly one expired, intact private bundle; never follow links."""
    current = _utc(now, "now")
    directory_fd = os.open(
        bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ConversationCollectionError("review_bundle_not_private")
        names = sorted(os.listdir(directory_fd))
        if names != ["manifest.json", "review.html"]:
            raise ConversationCollectionError("review_bundle_inventory_mismatch")
        manifest_bytes = _read_private_bundle_file(directory_fd, "manifest.json")
        html_bytes = _read_private_bundle_file(directory_fd, "review.html")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if not isinstance(manifest, dict):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        expires_at = manifest.get("expires_at")
        expected_html_hash = manifest.get("html_sha256")
        actual_html_hash = "sha256:" + hashlib.sha256(html_bytes).hexdigest()
        if not isinstance(expires_at, str) or expected_html_hash != actual_html_hash:
            raise ConversationCollectionError("review_bundle_integrity_error")
        try:
            expiry = _utc(
                datetime.fromisoformat(expires_at.replace("Z", "+00:00")),
                "expires_at",
            )
        except ValueError as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if current < expiry:
            return False
        os.unlink("review.html", dir_fd=directory_fd)
        os.unlink("manifest.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(bundle_dir)
    return True


def _read_private_bundle_file(directory_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
        ):
            raise ConversationCollectionError("review_bundle_file_not_private")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def run_approval_cli(
    argv: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    parser = argparse.ArgumentParser(
        description="Record privacy review for the exact bytes of an existing bundle."
    )
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--expected-html-sha256", required=True)
    parser.add_argument("--reviewer-binding-ref", required=True)
    args = parser.parse_args(list(argv))
    manifest = approve_private_review_bundle(
        bundle_dir=args.bundle_dir,
        expected_html_sha256=args.expected_html_sha256,
        reviewer_binding_ref=args.reviewer_binding_ref,
        now=now or datetime.now(UTC),
    )
    return {
        "status": "privacy_reviewed",
        "distribution_ready": False,
        "html_sha256": manifest.get("html_sha256"),
        "manifest_path": str(args.bundle_dir / "manifest.json"),
    }


def run_cli(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    collector_factory: Callable[..., ChatwootDailyCollector] = ChatwootDailyCollector,
) -> dict[str, object]:
    parser = argparse.ArgumentParser(
        description="Build a private daily conversation review package from Chatwoot."
    )
    parser.add_argument("--tenant-ref", required=True)
    parser.add_argument("--scope-ref", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(list(argv))

    required = (
        "CHATWOOT_BASE_URL",
        "CHATWOOT_ACCOUNT_ID",
        "CHATWOOT_INBOX_ID",
        "CHATWOOT_AGENT_BOT_ID",
        "CHATWOOT_API_ACCESS_TOKEN",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED",
        "DAILY_FEEDBACK_RETENTION_HOURS",
        "DAILY_FEEDBACK_DELETION_OWNER",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("missing_required_environment:" + ",".join(sorted(missing)))
    try:
        account_id = int(environ["CHATWOOT_ACCOUNT_ID"])
        inbox_id = int(environ["CHATWOOT_INBOX_ID"])
        agent_bot_id = int(environ["CHATWOOT_AGENT_BOT_ID"])
        pseudonymization_key = bytes.fromhex(
            environ["DAILY_FEEDBACK_PSEUDONYMIZATION_KEY"]
        )
        retention_hours = int(environ["DAILY_FEEDBACK_RETENTION_HOURS"])
        window_start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_daily_feedback_configuration") from exc
    if len(pseudonymization_key) != 32:
        raise ValueError("invalid_daily_feedback_pseudonymization_key")

    collector = collector_factory(
        base_url=environ["CHATWOOT_BASE_URL"],
        account_id=account_id,
        inbox_id=inbox_id,
        agent_bot_id=agent_bot_id,
        access_token=environ["CHATWOOT_API_ACCESS_TOKEN"],
        pseudonymization_key=pseudonymization_key,
        security_policy=RealConversationSecurityPolicy(
            real_collection_enabled=(
                environ["DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED"] == "true"
            ),
            storage_encryption_verified=(
                environ["DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED"] == "true"
            ),
            retention_hours=retention_hours,
            deletion_owner=environ["DAILY_FEEDBACK_DELETION_OWNER"],
        ),
    )
    package = collector.collect(
        tenant_ref=args.tenant_ref,
        scope_ref=args.scope_ref,
        window_start=window_start,
        window_end=window_end,
    )
    bundle = write_private_review_bundle(
        output_dir=args.output_dir,
        package=package,
    )
    return {
        "status": "created",
        "conversation_count": len(package.conversations),
        "distribution_ready": False,
        "html_path": str(bundle.html_path),
        "manifest_path": str(bundle.manifest_path),
    }


def _atomic_private_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
