# Johanna payment-link contract V2

## Status

This contract supersedes V1 for new sends. It describes the interface of the durable checkout issuance (migration `20260914000100`) and the offer resolution introduced by migration `20260922000100` (offer by lead intent). Enablement, deployment and E2E are operations recorded under `docs/operations/`, not here.

## Authorized catalog

Johanna has six active offers in the catalog, one per published landing, mirroring `public.johanna_precheckout_landing_offers`:

- tenant: `lancemos`
- funnel: `psicologajohanna`
- product/hotlink: `F106691755G`
- checkout base: `https://pay.hotmart.com/F106691755G`
- checkout mode: `10`
- source: `hermes`
- landings/offers: `ads-a`, `ads-b`, `ads-c`, `org-a`, `org-b`, `org-c`, each with the offer code the landing sends the person to
- default for inbound: `ads-a` only

The agent cannot choose or invent an offer or checkout. Zero active defaults fails closed; the database prevents two active defaults. A new catalog revision affects only new issuances.

## Offer resolution

The link repeats the offer the lead already saw. The reserve RPC resolves it from the lead's purchase intents of the funnel and records the outcome on the issuance row (`offer_resolution`, `lead_offer_code`):

- `lead_intent`: the lead has a live (`waiting_for_purchase`) intent whose landing/offer pair is an active catalog row. The link carries that offer. Intents backed by a real precheckout submission win over intents the RPC fabricated for earlier inbound links; among equals, the newest by `submitted_at`.
- `default_offer_not_in_catalog`: the lead has a live intent but its offer is not an active catalog row. The link carries the default; `lead_offer_code` keeps the offer the lead saw.
- `default_no_intent`: the lead has no live intent in the funnel (cold inbound). The link carries the default.
- `catalog_default`: rows issued before migration `20260922000100`.

A known purchase of the product by the same phone, through any offer, blocks the issuance (`purchase_already_approved`).

## URL and attribution

The exact V2 URL is:

```text
https://pay.hotmart.com/F106691755G?off=<offer>&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C<issuance_ulid>
```

`<offer>` is the resolved catalog offer. The decoded SCK is `hermes|v1|<issuance_ulid>`. It contains no `conversation_id`, contact identifier, phone, email, advertising SCK, UTM, or `fbclid`. V2 uses no PII prefill.

## Durable issuance

Before any Chatwoot POST, a reserve RPC creates one `checkout_link_issuances` row that binds the opaque ULID to tenant, case, purchase intent, conversation, contact, identity, product, offer, source kind, optional precheckout submission, optional original SCK, trigger message, exact URL, offer resolution, and delivery state. Failure to persist means no send.

Inbound requests create a purchase intent without fabricating a precheckout submission. A real matching precheckout intent is reused and its original SCK is read from the retained raw payload and stored only in the issuance row.

The unique conversation/message key makes retries reuse the same row, ULID, and URL. A replay never creates a second issuance or authorizes a blind second POST. Delivery states are `reserved`, `request_started`, `accepted_by_chatwoot`, `delivery_unknown`, and `purchase_matched`. After Chatwoot's live assignee/takeover checks, a second RPC reauthorizes durable state and transitions `reserved` to `request_started` immediately before the POST.

## Purchase correlation

For `PURCHASE_APPROVED`, Hotmart supplies SCK at `data.purchase.origin.sck`. The parser preserves it exactly. Only `hermes|v1|<valid-ulid>` values are eligible. Exact lookup marks the issuance and linked intent purchased. Missing, malformed, unknown, or conflicting values do not guess by conversation or buyer identity.

Correlation proves which issuance produced the sale, not that the original contact was the final buyer of a shared link.

## Safety and rollout

Immediate opt-out, known purchase, takeover/handoff, pause, replaced sequence, identity mismatch, conversation mismatch, stale trigger, invalid catalog, or persistence failure blocks before POST. URLs must not enter logs, errors, screenshots, or broad audit output.

Migration, deployment, enablement, live sending, and Hotmart purchase testing are separate operations. Final acceptance requires a controlled purchase whose Hotmart report returns the exact SCK.
