# Johanna payment-link contract V2

## Status

This contract supersedes V1 for new sends. It describes the interface of the durable checkout issuance (migration `20260914000100`), the offer resolution introduced by migration `20260922000100` (offer by lead intent), and the optional delivery of the URL in a WhatsApp template button (2026-10-01). Enablement, deployment and E2E are operations recorded under `docs/operations/`, not here.

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
https://pay.hotmart.com/F106691755G?off=<offer>&checkoutMode=10&src=hermes&sck=<sck>[&fbclid=<lead fbclid>]
```

`<offer>` is the resolved catalog offer.

The decoded SCK is `<ad sck>|hermes|v1|<issuance_ulid>` when the lead arrived with an advertising SCK, and `hermes|v1|<issuance_ulid>` otherwise. The ad's value is kept first and whole, so a parser that splits on `|` finds it in the leading field and the marker always closes the value. `fbclid` carries the lead's own click id and is present only when the lead arrived with one.

Both are propagated verbatim and only when they are safe to put in a query string: the ad SCK must match `[A-Za-z0-9._|~-]` and be at most 255 characters (200 until 2026-09-27: the Lancemos standard E02 appends `utm_id`, Meta's 18-digit campaign id, as a sixth field, `utm_source~utm_term~utm_content~utm_medium~utm_campaign~utm_id`; the longest ad SCK measured on Hotmart sales is 177 and the id takes it to 196, so the cap moved with margin; since 2026-09-25 the alphabet includes `~`, the separator the Lancemos core composes with from v1.10.0: `utm_source~utm_term~utm_content~utm_medium~utm_campaign`; `|` stays accepted because older lineages still emit it, and the hermes marker keeps its own `|`), the `fbclid` must match `[A-Za-z0-9._-]` and be at most 512. Anything else is dropped rather than escaped, and the issuance records which field was dropped. Nothing else travels: no `conversation_id`, contact identifier, phone, email, name, or UTM. V2 uses no PII prefill.

`attribution_resolution` on the issuance states what could be composed: `full`, `sck_only`, `fbclid_only`, or `marker_only`. The ad SCK and the `fbclid` are read from the lead's own precheckout submission, including when the resolved offer came from the catalog default because the lead's pair was uncatalogued.

## Durable issuance

Before any Chatwoot POST, a reserve RPC creates one `checkout_link_issuances` row that binds the opaque ULID to tenant, case, purchase intent, conversation, contact, identity, product, offer, source kind, optional precheckout submission, optional original SCK, trigger message, exact URL, offer resolution, and delivery state. Failure to persist means no send.

Inbound requests create a purchase intent without fabricating a precheckout submission. A real matching precheckout intent is reused and its original SCK is read from the retained raw payload and stored in the issuance row, both on its own column and as the leading field of the emitted SCK.

The unique conversation/message key makes retries reuse the same row, ULID, and URL. A replay never creates a second issuance or authorizes a blind second POST. Delivery states are `reserved`, `request_started`, `accepted_by_chatwoot`, `delivery_unknown`, and `purchase_matched`. After Chatwoot's live assignee/takeover checks, a second RPC reauthorizes durable state and transitions `reserved` to `request_started` immediately before the POST.

## Delivery in a template button

When the bridge is configured with a payment-link template name (`PAYMENT_LINK_TEMPLATE_NAME`, and optionally `PAYMENT_LINK_TEMPLATE_LANGUAGE`), the URL travels in the URL button of that WhatsApp template instead of being written in the message. Without a name, the URL is written in the message as before. This was requested on 2026-10-01: when the lead arrived from an ad, the URL measures 318 to 352 characters, and the `fbclid` alone takes 161 to 195 of them. WhatsApp's free-form URL button (`cta_url`) is not an option, because neither Chatwoot 4.13 nor 4.18.0 sends it.

The issued URL is not modified. The button suffix is the URL without `https://pay.hotmart.com/`, so the offer, `src`, SCK and `fbclid` all travel whole.

**The template.** The bridge reads the inbox catalog that Chatwoot publishes (`GET /inboxes/<id>`) on every delivery, and uses the template only if all of these hold:
- it is approved;
- its language equals the configured one;
- the header, body and footer have no placeholders;
- it has exactly one URL button, whose URL is `https://pay.hotmart.com/{{1}}`.

The category is whatever Meta assigned; `UTILITY` is the intent. `johanna_enlace_pago_01` was submitted as `UTILITY` and Meta approved it as `MARKETING` on 2026-10-01 (catalog capture in `tests/fixtures/chatwoot_message_templates_inbox_9_20261001.json`). The template only goes out in reply to the lead's request, inside the customer service window, where Meta applies neither the per-user marketing limit (`131049`) nor its marketing experiment (`130472`). A lead who turned off the business's marketing messages does not receive it (`131050`); see **Not handled**.

**Two parts.** The message goes out as two parts of the same reply batch, with the existing per-part idempotency:
1. The agent's text, with no URL and no issuance authorization.
2. The template, posted with `template_params`: `name`, `category`, `language`, and `processed_params.buttons[0] = {type: url, parameter: <suffix>}`. There is no `body` key, because the body has no placeholders.

The configured reply-part delay (`CHATWOOT_REPLY_PART_DELAY_SECONDS`, 2 s by default) separates the two parts, because Chatwoot sends each message to WhatsApp from its own job.

**Authorization.** The durable issuance is authorized at part 2, immediately before its POST, and finalized with part 2's Chatwoot message id. Retry semantics for the URL are unchanged.

**What the team sees.** Part 2's Chatwoot `content` is the template body, a newline, and the full URL. Chatwoot sends only the template to WhatsApp and shows `content` to the team. Readers that detect an agent link by `pay.hotmart.com` keep working, such as the coupon follow-up and the OS readers.

**Fallback to the written URL.** The URL is written in a single message, as before, in any of these cases:
- the template is missing or not approved;
- the template fails validation;
- the catalog read fails;
- the URL does not fit the button suffix, which must match `[A-Za-z0-9_-]+\?[A-Za-z0-9._~%=&-]+` and be at most 1,800 characters.

In each case the bridge logs `payment_link_template_unavailable reason=<code>` at WARNING. The log line carries no URL.

**Part 2 blocked after part 1.** If part 1 is delivered and part 2 is blocked by the live checks (a takeover, a pause, or a new message from the lead), the delivery is `blocked`, and the conversation goes to a human with that reason. The lead got the agent's text without the URL.

**Not handled.** If WhatsApp fails the template after Chatwoot accepted it (for example `131050`), the message turns `failed` in Chatwoot, and the bridge does not watch for that.

## Purchase correlation

For `PURCHASE_APPROVED`, Hotmart supplies SCK at `data.purchase.origin.sck`. The parser preserves it exactly. A value is eligible when the hermes marker closes it: either `hermes|v1|<valid-ulid>` or `<ad sck>|hermes|v1|<valid-ulid>`. The marker must be the suffix, so a lead supplied value cannot impersonate the tail the reporting reads. Exact lookup on the stored SCK marks the issuance and linked intent purchased. Missing, malformed, unknown, or conflicting values do not guess by conversation or buyer identity.

Correlation proves which issuance produced the sale, not that the original contact was the final buyer of a shared link.

## Safety and rollout

Immediate opt-out, known purchase, takeover/handoff, pause, replaced sequence, identity mismatch, conversation mismatch, stale trigger, invalid catalog, or persistence failure blocks before POST. URLs must not enter logs, errors, screenshots, or broad audit output.

Migration, deployment, enablement, live sending, and Hotmart purchase testing are separate operations. Final acceptance requires a controlled purchase whose Hotmart report returns the exact SCK.
