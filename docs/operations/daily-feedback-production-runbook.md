# Runbook — Daily Feedback Production V1

## Scope

Deploys one daily supervised report for Johanna. Chatwoot remains canonical. Supabase Cloud owns workflow state, authorization, decisions, leases, retention and tombstones. Slack receives one closed-catalog link per committed batch.

## Required initial policy inputs

- tenant, scope and Slack tenant route: confirm from canonical production authority; do not infer them from examples
- reviewer reference, Slack team and Slack user: confirm the authorized reviewer binding
- timezone: confirm the IANA timezone for the ally
- daily cutoff: confirm the local daily report cutoff
- retention and deletion owner: require explicit operational approval
- sanitizer: `deterministic-redaction-v1`
- selection: `chatwoot-daily-agent-dialogues-v1`
- renderer: `daily-feedback-web-v1`
- Conversation Release lineage: `release_lineage_unavailable` until runtime supplies canonical lineage

## Deployment order

1. Merge an immutable reviewed commit.
2. Apply `20260910000100_daily_feedback_production_v1.sql` to Supabase Cloud.
3. In the Slack application, enable Sign in with Slack/OpenID Connect and allow this exact redirect URL:

   ```text
   https://<daily-feedback-origin>/daily-feedback/auth/slack/callback
   ```

4. Deploy `deploy/daily-feedback.Dockerfile` as a dedicated HTTPS service with one replica and every variable in `deploy/daily-feedback.env.example`.
5. Configure the Slack connector with a server-owned tenant origin:

   ```text
   SLACK_TENANT_REVIEW_BASE_URLS_JSON={"johanna":"https://<daily-feedback-origin>"}
   ```

6. Redeploy the Slack connector from the same reviewed commit.
7. Require `/health=200` and `/ready=200` on both services before running a report.

Do not place tokens, OIDC codes, session values or credentials in URLs, logs, tickets or this runbook.

## Required authority inputs

- `DAILY_FEEDBACK_REVIEWER_SLACK_USER_ID` must be the canonical Slack member ID of the explicitly authorized reviewer.
- `SLACK_OIDC_TEAM_ID` must be the same workspace used by the private Johanna channel.
- `DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF` must identify the reviewed Supabase Cloud encryption-at-rest control.
- `DAILY_FEEDBACK_DELETION_OWNER` records the explicitly approved operational deletion responsibility.

Startup calls the idempotent configuration RPC. A changed reviewer binding increments its generation and invalidates existing browser sessions.

## Readiness behavior

The review service fails startup if any real-data, HTTPS, credential, reviewer, retention or encryption gate is absent. It becomes ready only after the durable authority configuration succeeds, a content-free Chatwoot probe confirms the canonical inbox/agent-bot binding, the Slack connector reports ready for the expected tenant, and the scheduler starts.

## Controlled run-now

Use the same scheduler path used by the daily worker:

```text
POST /internal/v1/daily-feedback/run
Authorization: Bearer <DAILY_FEEDBACK_MANUAL_RUN_TOKEN>
```

A successful response reports only `collected`, `notified` and `purged`. It never returns conversation content, reviewer identity or credentials.

## Expected Slack behavior

- event code: `REV-001`
- event ID: durable batch UUID
- one message per logical batch
- exactly one server-built HTTPS link
- `unfurl_links=false`
- `unfurl_media=false`
- exact admission retries reuse the same event ID and dedupe key

Slack is notification transport only. Decisions are submitted to the authenticated HTTPS application.

## Review flow

1. Open the clean batch URL.
2. Authenticate through Slack OpenID Connect.
3. The application validates the Slack team and creates a hashed, expiring server-side session only if the stored reviewer binding matches.
4. Each GET reauthorizes session, binding generation, tenant, scope, batch and retention in one SQL RPC.
5. Each POST additionally requires exact Origin, a session-derived CSRF token and an idempotent command ID.
6. Review one conversation at a time: `correct`, `correct_with_feedback`, or `skip`.
7. Feedback is required only for `correct_with_feedback` and is persisted literally.

No feedback automatically changes an agent or creates a Conversation Release.

## Retention and recovery

- Batch content and literal feedback expire after the configured, explicitly approved retention period.
- Purge deletes items, decisions, sessions and OIDC states, then writes a tombstone containing only counts, hashes, reason, actor and timestamps.
- Collection and Slack admission use database leases and fencing generations.
- A crash after request-start is reclaimed after lease expiry and retries the same connector event identity.
- Never manually repost a Slack report under a new event ID to resolve uncertainty.

## E2E acceptance

For the first real report, record without PII:

1. integrated commit SHA;
2. applied migration version;
3. review service and connector readiness;
4. run-now HTTP result;
5. durable batch UUID/public reference and item count;
6. one admitted Slack event and zero duplicate visible posts;
7. successful Slack login by the bound reviewer;
8. one test decision persisted and visible as completed count;
9. no pending/claimed/request-started/unknown notification rows;
10. purge/tombstone verification after expiry or an isolated shortened-retention test batch.

A healthy service or a generic Slack message is not sufficient evidence of this product path.
