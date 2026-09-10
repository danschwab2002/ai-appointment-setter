"""Fail-closed native Slack correlation interaction state machine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from slack_correlation.client import SlackProtocolError, SlackRejectedError
from slack_correlation.store import CorrelationBinding, NotificationStore, OpeningJob, ReviewSession
from slack_correlation.ui import (
    build_confirmation_modal,
    build_processing_modal,
    build_review_modal,
    build_safe_error_modal,
    build_success_modal,
    build_terminal_message,
)


class OperatorCorrelationClient(Protocol):
    async def get_case(self, case_id: str) -> dict[str, object]: ...
    async def prepare(self, **kwargs: object) -> dict[str, object]: ...
    async def confirm(self, **kwargs: object) -> dict[str, object]: ...


class SlackInteractionClient(Protocol):
    async def open_view(
        self, *, trigger_id: str, view: dict[str, Any], expected_team_id: str
    ) -> None: ...
    async def update_message(
        self, *, channel_id: str, message_ts: str, message: dict[str, Any]
    ) -> object: ...
    async def update_view(
        self, *, view_id: str, view_hash: str | None, view: dict[str, Any],
        expected_team_id: str,
    ) -> None: ...


class InvalidInteraction(ValueError):
    pass


class UnauthorizedInteraction(ValueError):
    """A well-shaped signed interaction whose local identity is not authorized."""


@dataclass(frozen=True)
class OpenAdmission:
    binding: CorrelationBinding
    team_id: str
    user_id: str
    trigger_id: str


@dataclass(frozen=True)
class PrepareAdmission:
    session: ReviewSession
    team_id: str
    user_id: str
    action: str
    candidate_id: str | None
    verification_basis: str
    view_id: str
    view_hash: str | None


@dataclass(frozen=True)
class ConfirmAdmission:
    session: ReviewSession
    team_id: str
    user_id: str
    view_id: str
    view_hash: str | None


InteractionAdmission = OpenAdmission | PrepareAdmission | ConfirmAdmission


def _safe_slack_extra(value: object, *, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 4096 and not any(
            (ord(character) < 32 and character != "\n") or ord(character) == 127
            for character in value
        )
    if isinstance(value, list):
        return len(value) <= 100 and all(
            _safe_slack_extra(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= 100 and all(
            isinstance(key, str)
            and 0 < len(key) <= 128
            and not any(ord(character) < 32 or ord(character) == 127 for character in key)
            and _safe_slack_extra(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


class CorrelationInteractionHandler:
    def __init__(
        self,
        *,
        store: NotificationStore,
        slack: SlackInteractionClient,
        operators: dict[str, OperatorCorrelationClient],
        team_id: str,
        tenant_channels: dict[str, str],
        tenant_user_ids: dict[str, frozenset[str]],
        epoch_clock,
    ) -> None:
        self._store = store
        self._slack = slack
        self._operators = dict(operators)
        self._team_id = team_id
        self._tenant_by_channel = {channel: tenant for tenant, channel in tenant_channels.items()}
        self._users = tenant_user_ids
        self._epoch = epoch_clock

    def precheck(self, payload: object) -> InteractionAdmission:
        """Validate shape, authorization and binding without network or mutation."""
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise InvalidInteraction("invalid_payload")
        if payload["type"] == "block_actions":
            allowed = {
                "type", "team", "user", "channel", "message", "trigger_id",
                "actions", "api_app_id", "container", "response_url", "enterprise",
                "token", "state", "response_urls", "enterprise_id", "is_enterprise_install",
                "bot_access_token", "function_data", "interactivity",
            }
            required = {"type", "team", "user", "channel", "message", "trigger_id", "actions"}
            if not required <= set(payload) or set(payload) - allowed:
                raise InvalidInteraction("invalid_payload_shape")
            if any(
                key in payload and not _safe_slack_extra(payload[key])
                for key in allowed - required
            ):
                raise InvalidInteraction("invalid_payload_shape")
            return self._precheck_open(payload)
        if payload["type"] == "view_submission":
            allowed = {
                "type", "team", "user", "view", "api_app_id", "trigger_id", "enterprise",
                "token", "response_urls", "enterprise_id", "is_enterprise_install",
                "bot_access_token", "function_data", "interactivity",
            }
            if not {"type", "team", "user", "view"} <= set(payload) or set(payload) - allowed:
                raise InvalidInteraction("invalid_payload_shape")
            if any(
                key in payload and not _safe_slack_extra(payload[key])
                for key in allowed - {"type", "team", "user", "view"}
            ):
                raise InvalidInteraction("invalid_payload_shape")
            view = payload.get("view")
            if not isinstance(view, dict):
                raise InvalidInteraction("invalid_view")
            self._validate_view(view)
            team_id, user_id = self._team_user(payload)
            if not any(user_id in users for users in self._users.values()):
                raise UnauthorizedInteraction("unauthorized_user")
            callback = view.get("callback_id")
            if callback == "prepare_operator_correlation_resolution":
                return self._precheck_prepare(payload, view, team_id, user_id)
            if callback == "confirm_operator_correlation_resolution":
                return self._precheck_confirm(payload, view, team_id, user_id)
        raise InvalidInteraction("unsupported_interaction")

    def admit(
        self, *, fingerprint: str, admission: InteractionAdmission
    ) -> tuple[int, dict[str, object]]:
        """Atomically reserve replay identity and durably admit local work."""
        if isinstance(admission, OpenAdmission):
            _new, status, response, _token = self._store.admit_open_interaction(
                fingerprint=fingerprint,
                binding=admission.binding,
                team_id=admission.team_id,
                slack_user_id=admission.user_id,
                trigger_id=admission.trigger_id,
                expires_at=int(self._epoch()) + 900,
            )
        elif isinstance(admission, PrepareAdmission):
            response = {
                "response_action": "update",
                "view": build_processing_modal(
                    review_token=admission.session.review_token, phase="prepare"
                ),
            }
            _new, status, response = self._store.admit_prepare_interaction(
                fingerprint=fingerprint,
                review_token=admission.session.review_token,
                team_id=admission.team_id,
                slack_user_id=admission.user_id,
                now_epoch=int(self._epoch()),
                action=admission.action,
                candidate_id=admission.candidate_id,
                verification_basis=admission.verification_basis,
                view_id=admission.view_id,
                view_hash=admission.view_hash,
                response=response,
            )
        else:
            response = {
                "response_action": "update",
                "view": build_processing_modal(
                    review_token=admission.session.review_token, phase="confirm"
                ),
            }
            _new, status, response = self._store.admit_confirm_interaction(
                fingerprint=fingerprint,
                review_token=admission.session.review_token,
                team_id=admission.team_id,
                slack_user_id=admission.user_id,
                now_epoch=int(self._epoch()),
                view_id=admission.view_id,
                view_hash=admission.view_hash,
                response=response,
            )
        return status or 200, response or {}

    def _validate_view(self, view: dict[str, object]) -> None:
        required = {"private_metadata", "callback_id", "state"}
        if not required <= set(view) or not _safe_slack_extra(view):
            raise InvalidInteraction("invalid_view")
        if (
            not isinstance(view.get("private_metadata"), str)
            or not isinstance(view.get("callback_id"), str)
            or not isinstance(view.get("state"), dict)
            or set(view["state"]) != {"values"}  # type: ignore[arg-type]
            or not isinstance(view["state"].get("values"), dict)  # type: ignore[union-attr]
            or (view.get("team_id") is not None and view.get("team_id") != self._team_id)
            or (view.get("type") is not None and view.get("type") != "modal")
            or (view.get("id") is not None and not isinstance(view.get("id"), str))
            or (view.get("hash") is not None and not isinstance(view.get("hash"), str))
        ):
            raise InvalidInteraction("invalid_view")

    def _team_user(self, payload: dict[str, object]) -> tuple[str, str]:
        team = payload.get("team")
        user = payload.get("user")
        if (
            not isinstance(team, dict) or "id" not in team or not _safe_slack_extra(team)
            or not isinstance(user, dict) or "id" not in user or not _safe_slack_extra(user)
        ):
            raise InvalidInteraction("invalid_identity")
        team_id, user_id = team.get("id"), user.get("id")
        if (
            team_id != self._team_id or not isinstance(user_id, str)
            or (user.get("team_id") is not None and user.get("team_id") != team_id)
        ):
            raise InvalidInteraction("invalid_identity")
        return team_id, user_id

    def _precheck_open(self, payload: dict[str, object]) -> OpenAdmission:
        team_id, user_id = self._team_user(payload)
        channel = payload.get("channel")
        message = payload.get("message")
        actions = payload.get("actions")
        trigger_id = payload.get("trigger_id")
        if (
            not isinstance(channel, dict) or not isinstance(channel.get("id"), str)
            or not _safe_slack_extra(channel)
            or not isinstance(message, dict) or not isinstance(message.get("ts"), str)
            or not _safe_slack_extra(message)
            or not isinstance(actions, list) or len(actions) != 1
            or not isinstance(actions[0], dict) or not _safe_slack_extra(actions[0])
            or actions[0].get("action_id") != "review_operator_correlation"
            or not isinstance(actions[0].get("value"), str)
            or not isinstance(trigger_id, str) or not trigger_id
        ):
            raise InvalidInteraction("invalid_action")
        channel_id = channel["id"]
        tenant = self._tenant_by_channel.get(channel_id)
        if tenant is None or user_id not in self._users.get(tenant, frozenset()):
            raise UnauthorizedInteraction("unauthorized_user")
        binding = self._store.find_correlation_binding(
            tenant_ref=tenant,
            team_id=team_id,
            channel_id=channel_id,
            message_ts=str(message["ts"]),
        )
        if binding is None or actions[0]["value"] != binding.case_id:
            raise UnauthorizedInteraction("unauthorized_binding")
        return OpenAdmission(
            binding=binding, team_id=team_id, user_id=user_id, trigger_id=trigger_id
        )

    def _bound_session(self, payload: dict[str, object], view: dict[str, object]) -> ReviewSession:
        team_id, user_id = self._team_user(payload)
        try:
            metadata = __import__("json").loads(view["private_metadata"])
            if set(metadata) != {"review_token"}:
                raise ValueError
            token = str(UUID(metadata["review_token"]))
        except (KeyError, TypeError, ValueError):
            raise InvalidInteraction("invalid_review_token") from None
        session = self._store.get_review_session(review_token=token)
        if session is not None and user_id not in self._users.get(
            session.tenant_ref, frozenset()
        ):
            raise UnauthorizedInteraction("unauthorized_user")
        if (
            session is None or session.team_id != team_id or session.slack_user_id != user_id
            or session.expires_at < int(self._epoch())
            or self._tenant_by_channel.get(session.channel_id) != session.tenant_ref
        ):
            raise InvalidInteraction("invalid_review_session")
        return session

    def _precheck_prepare(
        self, payload: dict[str, object], view: dict[str, object],
        team_id: str, user_id: str,
    ) -> PrepareAdmission:
        session = self._bound_session(payload, view)
        values = view.get("state")
        try:
            state_values = values["values"]  # type: ignore[index]
            if set(state_values) != {"resolution", "verification"}:
                raise KeyError
            selected = state_values["resolution"]["selected_resolution"]["selected_option"]["value"]
            verification = state_values["verification"]["verification_basis"]["selected_option"]["value"]
        except (KeyError, TypeError):
            raise InvalidInteraction("invalid_selection") from None
        allowed_basis = {
            "external_transaction_reference", "operator_source_record",
            "customer_confirmation", "no_valid_candidate_after_review",
        }
        if not isinstance(selected, str) or verification not in allowed_basis:
            raise InvalidInteraction("invalid_selection")
        if selected == "close_without_match":
            action, candidate_id = selected, None
            if verification != "no_valid_candidate_after_review":
                raise InvalidInteraction("invalid_selection")
        else:
            action, candidate_id = "resolve_with_candidate", selected
            try:
                candidate_id = str(UUID(selected))
            except ValueError:
                raise InvalidInteraction("invalid_selection") from None
            if verification == "no_valid_candidate_after_review":
                raise InvalidInteraction("invalid_selection")
        view_id = view.get("id")
        view_hash = view.get("hash")
        if not isinstance(view_id, str) or not view_id:
            raise InvalidInteraction("invalid_view_identity")
        return PrepareAdmission(
            session=session,
            team_id=team_id,
            user_id=user_id,
            action=action,
            candidate_id=candidate_id,
            verification_basis=str(verification),
            view_id=view_id,
            view_hash=view_hash if isinstance(view_hash, str) else None,
        )

    def _precheck_confirm(
        self, payload: dict[str, object], view: dict[str, object],
        team_id: str, user_id: str,
    ) -> ConfirmAdmission:
        session = self._bound_session(payload, view)
        view_id = view.get("id")
        view_hash = view.get("hash")
        if not isinstance(view_id, str) or not view_id:
            raise InvalidInteraction("invalid_view_identity")
        return ConfirmAdmission(
            session=session,
            team_id=team_id,
            user_id=user_id,
            view_id=view_id,
            view_hash=view_hash if isinstance(view_hash, str) else None,
        )


class CorrelationInteractionWorker:
    """Resume idempotent prepare/confirm effects from durable session states."""

    def __init__(self, *, store: NotificationStore, slack: SlackInteractionClient,
                 operators: dict[str, OperatorCorrelationClient], team_id: str,
                 poll_interval: float = 0.1) -> None:
        self._store = store
        self._slack = slack
        self._operators = operators
        self._team_id = team_id
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="slack-interaction-worker")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            opening_jobs = await asyncio.to_thread(self._store.pending_opening_jobs)
            for job in opening_jobs:
                try:
                    await self._open(job)
                except Exception:
                    # Pre-request failures retain pending state and may be retried.
                    pass
            sessions = await asyncio.to_thread(self._store.active_review_sessions)
            for session in sessions:
                try:
                    if session.state == "preparing":
                        await self._prepare(session)
                    elif session.state == "confirming":
                        await self._confirm(session)
                except Exception:
                    # Durable state is intentionally retained for an idempotent retry.
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    async def _open(self, job: OpeningJob) -> None:
        session = self._store.get_review_session(review_token=job.review_token)
        if session is None or session.state != "opened":
            self._store.fail_opening(review_token=job.review_token)
            return
        case = await self._operators[session.tenant_ref].get_case(session.case_id)
        if case.get("case_id") != session.case_id:
            self._store.fail_opening(review_token=session.review_token)
            return
        view = build_review_modal(case, review_token=session.review_token)
        self._store.mark_open_request_started(review_token=session.review_token)
        try:
            await self._slack.open_view(
                trigger_id=job.trigger_id,
                view=view,
                expected_team_id=self._team_id,
            )
        except Exception:
            # views.open is ambiguous after request start; never retry it blindly.
            self._store.fail_opening(review_token=session.review_token)
            return
        self._store.finish_opening(review_token=session.review_token)

    async def _prepare(self, session: ReviewSession) -> None:
        case = await self._operators[session.tenant_ref].get_case(session.case_id)
        if case.get("case_id") != session.case_id:
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        candidates = {
            item.get("purchase_intent_id")
            for item in case.get("candidates", [])
            if isinstance(item, dict)
            and item.get("lifecycle_state") == "waiting_for_purchase"
        }
        if session.action == "resolve_with_candidate" and session.candidate_id not in candidates:
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        command = await self._operators[session.tenant_ref].prepare(
            case_id=session.case_id,
            idempotency_key=session.idempotency_key,
            action=session.action,
            candidate_id=session.candidate_id,
            verification_basis=session.verification_basis,
            actor_ref=f"slack.{session.slack_user_id.lower()}",
        )
        try:
            command_id = str(UUID(str(command.get("command_id"))))
        except ValueError:
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        if (
            command_id != command.get("command_id")
            or command.get("case_id") != session.case_id
            or command.get("idempotency_key") != session.idempotency_key
            or command.get("action") != session.action
            or command.get("candidate_id") != session.candidate_id
            or command.get("verification_basis") != session.verification_basis
        ):
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        self._store.finish_prepare(review_token=session.review_token, command=command)
        if session.view_id:
            await self._slack.update_view(
                view_id=session.view_id,
                view_hash=None,
                view=build_confirmation_modal(
                    review_token=session.review_token, action=session.action or ""
                ),
                expected_team_id=self._team_id,
            )

    async def _confirm(self, session: ReviewSession) -> None:
        command = session.prepared_command
        if command is None or not isinstance(command.get("command_id"), str):
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        result = await self._operators[session.tenant_ref].confirm(
            command_id=command["command_id"],
            expected_action=session.action,
            expected_candidate_id=session.candidate_id,
            actor_ref=f"slack.{session.slack_user_id.lower()}",
        )
        expected_outcome = (
            "linked_candidate" if session.action == "resolve_with_candidate"
            else "closed_without_match"
        )
        if (
            result.get("case_id") != session.case_id
            or result.get("automation_blocked") is not True
            or result.get("resolution_outcome") != expected_outcome
            or result.get("effective_purchase_intent_id") != session.candidate_id
            or not isinstance(result.get("applied_at"), str)
        ):
            self._store.fail_session(review_token=session.review_token)
            await self._show_error(session)
            return
        binding = self._store.finish_resolution_and_begin_projection(
            review_token=session.review_token, result=result
        )
        terminal = build_terminal_message(
            case_id=session.case_id,
            outcome=str(result["resolution_outcome"]),
            actor_id=f"slack.{session.slack_user_id.lower()}",
            applied_at=str(result["applied_at"]),
        )
        try:
            await self._slack.update_message(
                channel_id=session.channel_id,
                message_ts=session.message_ts,
                message=terminal,
            )
        except SlackRejectedError:
            self._store.finish_projection(
                binding=binding, state="rejected", failure_code="slack_rejected"
            )
        except SlackProtocolError:
            self._store.finish_projection(
                binding=binding,
                state="delivery_unknown",
                failure_code="slack_update_unknown",
            )
        else:
            self._store.finish_projection(binding=binding, state="accepted")
        if session.view_id:
            await self._slack.update_view(
                view_id=session.view_id,
                view_hash=None,
                view=build_success_modal(),
                expected_team_id=self._team_id,
            )

    async def _show_error(self, session: ReviewSession) -> None:
        if session.view_id:
            await self._slack.update_view(
                view_id=session.view_id,
                view_hash=None,
                view=build_safe_error_modal(),
                expected_team_id=self._team_id,
            )
