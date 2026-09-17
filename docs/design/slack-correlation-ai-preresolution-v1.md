# Slack correlation AI pre-resolution V1

## Purpose

Reduce identity-correlation Slack work to a bounded human confirmation. Preserve
unmatched cases for observability without notifying them. Never let a model
resolve a case or authorize a commercial effect.

## State flow

```text
unresolved correlation
  unmatched -> suppressed_unmatched
  ambiguous/conflict -> pending -> leased
    model/policy failure -> retryable_failed -> leased
    max retries -> terminal_failed
    valid recommendation -> recommended -> Slack contract V3
    abstention -> abstained
```

Only `recommended` creates a new Slack projection. `abstained`,
`suppressed_unmatched`, and `terminal_failed` remain queryable but do not notify.

## Trust boundaries

- Supabase owns durable admission, snapshots, leases, fencing, attempts and the
  recommendation-to-projection gate.
- The bridge owns evidence construction, model invocation and deterministic
  validation.
- The Slack connector only validates and renders immutable V3 payloads.
- Existing operator `prepare -> confirm` remains the sole resolution authority.
- The model has no resolution client, message sender, consent writer or recovery
  dependency.

## Evidence contract

The model receives only:

- canonical case/event/outcome enums;
- opaque candidate UUIDs and `Persona N` labels;
- bounded evidence enums;
- bounded numeric time gaps;
- server-derived `independent` and `discriminating` booleans.

It does not receive names, email addresses, phone numbers, webhook free text,
Chatwoot messages, secrets or raw payloads. The first evidence read persists an
immutable JSON snapshot and SHA-256 fingerprint. Retries reuse that snapshot.

A recommendation is publishable only when it:

- names one candidate from the snapshot;
- reports high confidence;
- references only known evidence rows for that candidate;
- has no referenced contradiction;
- contains at least one server-derived independent and discriminating fact.

Prompt `correlation-preresolution-v2` states the same executable meaning as the
validator: a fact with
`independent=true` and `discriminating=true` is bridge-verified evidence that
clearly distinguishes its candidate. If exactly one candidate has such evidence
and there is no contradiction, the model is instructed to recommend that
candidate with high confidence and cite the fact.

Prompt `correlation-preresolution-v3` preserves that policy and makes the output
contract explicit. In particular, `decision` is exactly `recommend_candidate`
or `abstain`; the shorter alias `recommend` is invalid. It also pins confidence
tokens and requires each evidence or missing-information field to be a JSON
array of strings, using `[]` rather than `null`. On abstention, evidence arrays
must be empty and missing-information entries must be bounded lowercase machine
tokens rather than prose. A recommendation carrying any missing-information
token is rejected. The deterministic parser remains strict and does not
normalize aliases.

Everything else becomes abstention. `same_product_offer`, event email matches and
event phone matches are never sufficient discriminators. Every terminal
abstention persists one allowlisted, non-PII `decision_reason_code`; raw model
text and `missing_information` values are not persisted. Existing abstentions
are backfilled as `legacy_unclassified`.

## Replay compatibility

Contracts 1 and 2 retain their exact payload and UUID rules for already-attempted
historical rows. The migration gate prevents creation of new generic projections.
Contract 3 requires a durable recommendation and binds recommendation reference,
candidate, evidence, evidence fingerprint, model and prompt version into the
connector payload and semantic hash. Exact replay deduplicates; changed semantic
material conflicts instead of silently replacing an admitted message.

## Rollout

1. Stop bridge projection workers before applying the migration.
2. Inventory nonterminal V1/V2 projection and connector rows. Reconcile
   `request_started` or `delivery_unknown`; do not retry them blindly.
3. Apply `20260916000100_operator_correlation_ai_preresolution.sql`.
   Apply `20260917000100_operator_correlation_abstention_reason.sql` before
   deploying a bridge that sends `decision_reason_code`. The migration retains
   the eight-argument completion RPC during the rolling deploy and records its
   abstentions as `legacy_unclassified`.
4. Deploy the bridge and connector with both pre-resolution and Slack projection
   disabled. Set `CORRELATION_PRERESOLUTION_PROMPT_VERSION` to
   `correlation-preresolution-v3`; V1 and V2 remain accepted only for exact
   historical behavior and rollback.
5. Verify `/health`, `/ready`, ACLs, RPC signatures and zero invalid leases.
6. Enable `CORRELATION_PRERESOLUTION_ENABLED` first.
7. Confirm synthetic unmatched becomes `suppressed_unmatched` with zero model and
   connector calls.
8. Confirm synthetic abstention creates no connector delivery.
9. Confirm one synthetic recommendation creates exactly one V3 Slack card.
10. Enable Slack projection and complete the allowlisted human review E2E.

`PURCHASE_CANCELED` remains inactive until its source, projection and backend
actions are approved and validated separately.

## Rollback

Disable `SLACK_CONNECTOR_PROJECTION_ENABLED` first, then disable
`CORRELATION_PRERESOLUTION_ENABLED`. Do not remove durable rows or re-enable old
V1/V2 admission. Preserve proposal and connector ledgers for reconciliation.
