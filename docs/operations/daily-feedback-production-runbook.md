# Runbook — Daily Feedback Production V1

## Scope

Deploys one daily supervised report for Johanna. Chatwoot remains canonical. Supabase Cloud owns workflow state, authorization, decisions, leases, retention and tombstones. Slack receives one closed-catalog link per committed batch.

## Required initial policy inputs

- tenant, scope and Slack tenant route: confirm from canonical production authority; do not infer them from examples
- complete reviewer set: exactly four reviewer refs and Slack member IDs, all deletion-accountable, plus the Slack team
- timezone: confirm the IANA timezone for the ally
- daily cutoff: confirm the local daily report cutoff
- retention, deletion policy and accountability of all four reviewers: require explicit operational approval
- sanitizer: `deterministic-redaction-v1`
- selection: `chatwoot-daily-agent-dialogues-v1`
- renderer: `daily-feedback-web-v1`
- Conversation Release lineage: `release_lineage_unavailable` until runtime supplies canonical lineage
- Chatwoot gate: the inbox-scoped `agent_bot` endpoint must return the configured bot ID; an account-wide bot match is insufficient

## Deployment order

1. Merge an immutable reviewed commit.
2. Apply `20260910000100_daily_feedback_production_v1.sql`,
   `20260911000100_daily_feedback_notification_fencing_v1.sql`, and
   `20260911000200_daily_feedback_multi_reviewer_ownership_v1.sql` in order to
   Supabase Cloud through the official migration workflow.
3. In the Slack application, enable Sign in with Slack/OpenID Connect and allow this exact redirect URL:

   ```text
   https://<daily-feedback-origin>/daily-feedback/auth/slack/callback
   ```

4. Deploy `deploy/daily-feedback.Dockerfile` as a dedicated HTTPS service with one replica and every variable in `deploy/daily-feedback.env.example`; keep `DAILY_FEEDBACK_SCHEDULER_ENABLED=false`.
5. Configure the Slack connector with a server-owned tenant origin:

   ```text
   SLACK_TENANT_REVIEW_BASE_URLS_JSON={"johanna":"https://<daily-feedback-origin>"}
   ```

6. Redeploy the Slack connector from the same reviewed commit.
7. Require `/health=200` and `/ready=200` on both services before running a report.
8. Run the first real report only through the authenticated run-now endpoint and complete the E2E checks below.
9. Set `DAILY_FEEDBACK_SCHEDULER_ENABLED=true` only after the batch, single Slack post, reviewer authorization, durable decision, expiration, tombstone and purge checks pass.

The flag disables both the background polling task and the durable schedule for
ordinary claims. The authenticated run-now call alone sends `force=true`; the SQL
RPC permits that controlled claim only after rechecking the exact reviewer set.

Do not place tokens, OIDC codes, session values or credentials in URLs, logs, tickets or this runbook.

## Required authority inputs

- `DAILY_FEEDBACK_REVIEWERS_JSON` is the complete desired set. Every object has exactly `reviewer_ref`, canonical `slack_user_id`, and boolean `deletion_accountable`; unknown keys, duplicates, an empty set or no accountable reviewer fail closed.
- `SLACK_OIDC_TEAM_ID` must be the same workspace used by the private Johanna channel.
- `DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF` must identify the reviewed Supabase Cloud encryption-at-rest control.
- `DAILY_FEEDBACK_DELETION_POLICY_REF` names the approved policy. Human accountability is snapshotted from the reviewer objects; the purge worker is recorded separately.

Startup calls `configure_daily_feedback_scope_v2`. A changed reviewer binding increments only that person's generation and invalidates only that person's browser sessions. The legacy V1 configuration RPC has no service-role execute grant and cannot collapse the set.

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
3. The application validates the Slack team and creates a hashed, expiring server-side session only if the reviewer is in the immutable batch snapshot and the live binding still matches.
4. Each GET reauthorizes session, snapshotted binding generation, live binding, tenant, scope, batch and retention in one SQL RPC.
5. Each POST additionally requires exact Origin, a session-derived CSRF token and an idempotent command ID.
6. Review one conversation at a time: `correct`, `correct_with_feedback`, or `skip`.
7. Feedback is required only for `correct_with_feedback` and is persisted literally.

No feedback automatically changes an agent or creates a Conversation Release.

## Retention and recovery

- Batch content and literal feedback expire after the configured, explicitly approved retention period.
- Purge uses PostgreSQL server time as the retention authority, rejects null or out-of-range limits, deletes items, decisions, sessions, OIDC states and batch-reviewer mappings, then writes an immutable tombstone containing only counts, hashes, policy, snapshotted reviewer identities/generations, purge actor and timestamps.
- Notification retries accept only closed lowercase error codes and delays from 1 to 900 seconds. Never retry `delivery_unknown` without operator reconciliation.
- Collection and Slack admission use database leases and fencing generations.
- A crash after request-start is reclaimed after lease expiry and retries the same connector event identity.
- Never manually repost a Slack report under a new event ID to resolve uncertainty.

## E2E acceptance

For the first real report, record without PII:

1. integrated commit SHA;
2. applied migration versions and official migration-ledger agreement;
3. review service and connector readiness;
4. run-now HTTP result;
5. durable batch UUID/public reference and item count;
6. one admitted Slack event and zero duplicate visible posts;
7. successful Slack login by every configured reviewer, rejection of an unconfigured identity, and no retroactive access for a reviewer added after batch commit;
8. one test decision persisted and visible as completed count;
9. no pending/claimed/request-started/unknown notification rows;
10. expiration, purge and tombstone verification, including accountable reviewer refs and a separate purge actor, after expiry or an isolated shortened-retention test batch;
11. scheduler remains disabled through all prior checks and is enabled only afterward.

A healthy service or a generic Slack message is not sufficient evidence of this product path.
