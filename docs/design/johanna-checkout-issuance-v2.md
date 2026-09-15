# Johanna checkout issuance V2 design

## Decision

A checkout request is a commercial intent whether it follows `lead.precheckout` or begins in Chatwoot. The runtime resolves one server-owned offer and persists a durable issuance before sending.

## Components

### `checkout_offer_catalog`

Append-only catalog revisions hold the authorized product, landing, offer, checkout base, mode, and approval evidence. Only `active -> retired` is permitted on an existing row. A partial unique index permits at most one active default per tenant/product.

### `checkout_link_issuances`

One row per Chatwoot trigger message. `issuance_ulid` is a random opaque lookup key; it encodes no business data. The row snapshots the chosen catalog revision and the exact URL. It also references the case, intent, contact, channel identity, conversation, optional precheckout submission, and Hotmart event.

### RPC boundary

`reserve_chatwoot_checkout_issuance_v2`:

1. acquires an advisory lock for conversation/message;
2. returns an existing issuance on replay;
3. revalidates case, published scope, account, inbox, conversation, identity, contact, opt-out, known purchase, and exactly one active catalog default;
4. reuses a matching open intent or creates an inbound intent;
5. reads `original_sck` only from a real precheckout raw payload;
6. inserts the issuance with the exact URL and state `reserved`;
7. returns the persisted snapshot.

`authorize_chatwoot_checkout_issuance_v2` rechecks scope, contact, opt-out, conversation, takeover, identity, and purchase immediately before the POST, then transitions `reserved` to `request_started`. It does not re-resolve the catalog: retiring an offer blocks new reservations but preserves already-reserved snapshots.

`finalize_chatwoot_checkout_issuance_v2` records `accepted_by_chatwoot` only with a provider message ID; otherwise it records `delivery_unknown`. Unknown delivery never becomes retry authorization, but an exact existing Chatwoot message may reconcile it to accepted without another POST.

`admit_and_correlate_hotmart_checkout_issuance_v2` validates the strict decoded SCK grammar and exact payload-to-SCK equality before webhook admission, then invokes the owner-only `correlate_hotmart_checkout_issuance_v2` helper to perform the unique lookup and mark the linked intent purchased. Replays of the same event are idempotent; a different event cannot overwrite an existing match.

All RPCs are `SECURITY DEFINER` and pin `search_path`. Externally callable orchestration RPCs expose `EXECUTE` only to `service_role`; the core correlation helper explicitly revokes `service_role` so callers cannot bypass the payload-bound admission wrapper. Both tables have RLS enabled and no direct API grants.

## Runtime flow

```text
inbound/precheckout request
  -> active commercial case + immediate authorization
  -> reserve RPC (intent + durable issuance)
  -> Chatwoot live authorization
  -> authorize RPC
  -> Chatwoot POST using persisted URL
  -> finalize RPC

Hotmart PURCHASE_APPROVED
  -> validated event persistence
  -> read data.purchase.origin.sck
  -> exact SCK correlation RPC
  -> issuance purchase_matched + intent purchased
```

The existing `PAYMENT_LINK_ENABLED` flag remains the outer runtime gate. The migration itself does not enable anything.

## Compatibility

V1 `payment_link_bindings` and commands remain unchanged for historical truth. V2 uses separate rows and RPCs. No existing delivery-unknown record is retried or rewritten. New runtime wiring may select V2 only after the migration is present; disabling the feature continues to block sends.

## Verification

A PGlite validator applies the complete migration stack and proves:

- the exact Johanna catalog row;
- inbound intent creation without a submission;
- precheckout reuse with durable `original_sck`;
- exact encoded URL and decoded SCK;
- same-row replay;
- accepted delivery finalization;
- exact Hotmart purchase correlation;
- table and function ACL isolation.

A later controlled E2E must verify Hotmart reports the exact SCK. Production rollout remains out of scope until separately authorized.
