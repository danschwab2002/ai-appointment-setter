# Johanna payment-link contract V2

## Status

This contract supersedes V1 for new sends only after migration, deployment, a controlled E2E, and explicit enablement. Runtime wiring is implemented and verified locally. Existing V1 records remain auditable. `PAYMENT_LINK_ENABLED=false` is the stable state.

## Authorized catalog

Johanna has exactly one active default offer for new links:

- tenant: `lancemos`
- funnel: `psicologajohanna`
- product/hotlink: `F106691755G`
- landing: `ads-a`
- offer: `bxjge6zq`
- checkout base: `https://pay.hotmart.com/F106691755G`
- checkout mode: `10`
- source: `hermes`

The agent cannot choose or invent an offer or checkout. Zero active defaults fails closed; the database prevents two active defaults. A new catalog revision affects only new issuances.

## URL and attribution

The exact V2 URL is:

```text
https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C<issuance_ulid>
```

The decoded SCK is `hermes|v1|<issuance_ulid>`. It contains no `conversation_id`, contact identifier, phone, email, advertising SCK, UTM, or `fbclid`. V2 uses no PII prefill.

## Durable issuance

Before any Chatwoot POST, a reserve RPC creates one `checkout_link_issuances` row that binds the opaque ULID to tenant, case, purchase intent, conversation, contact, identity, product, offer, source kind, optional precheckout submission, optional original SCK, trigger message, exact URL, and delivery state. Failure to persist means no send.

Inbound requests create a purchase intent without fabricating a precheckout submission. A real matching precheckout intent is reused and its original SCK is read from the retained raw payload and stored only in the issuance row.

The unique conversation/message key makes retries reuse the same row, ULID, and URL. A replay never creates a second issuance or authorizes a blind second POST. Delivery states are `reserved`, `request_started`, `accepted_by_chatwoot`, `delivery_unknown`, and `purchase_matched`. After Chatwoot's live assignee/takeover checks, a second RPC reauthorizes durable state and transitions `reserved` to `request_started` immediately before the POST.

## Purchase correlation

For `PURCHASE_APPROVED`, Hotmart supplies SCK at `data.purchase.origin.sck`. The parser preserves it exactly. Only `hermes|v1|<valid-ulid>` values are eligible. Exact lookup marks the issuance and linked intent purchased. Missing, malformed, unknown, or conflicting values do not guess by conversation or buyer identity.

Correlation proves which issuance produced the sale, not that the original contact was the final buyer of a shared link.

## Safety and rollout

Immediate opt-out, known purchase, takeover/handoff, pause, replaced sequence, identity mismatch, conversation mismatch, stale trigger, invalid catalog, or persistence failure blocks before POST. URLs must not enter logs, errors, screenshots, or broad audit output.

Migration, deployment, enablement, live sending, and Hotmart purchase testing are separate operations. Final acceptance requires a controlled purchase whose Hotmart report returns the exact SCK.
