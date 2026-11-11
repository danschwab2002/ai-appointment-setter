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
- `campaign` (E-410, migration `20261110000500`): the link carries the offer of the campaign of the turn. Only `reserve_portable_checkout_issuance_for_offer_v1` writes it, and the row keeps the campaign key in `campaign_ref` (non-null exactly when `offer_resolution = 'campaign'`, immutable like the rest of the row). `lead_offer_code` keeps the offer of the lead's live intent, if any.

### The reserve with the offer of a campaign (E-410)

`reserve_portable_checkout_issuance_for_offer_v1(p_commercial_case_id, p_external_user_id, p_chatwoot_account_id, p_chatwoot_inbox_id, p_chatwoot_conversation_id, p_trigger_external_message_id, p_issuance_ulid, p_funnel_ref, p_offer_code, p_campaign_ref, p_now)`, `service_role` only. It is derived from the live definition of `reserve_portable_checkout_issuance_for_funnel_v1` with counted replacements (the migration aborts if that definition changed) and keeps every check of it: case, scope, the funnel binding in the inbox, contact, opt-out, conversation, identity and the purchase of the product through any offer.

- `p_offer_code` (`^[A-Za-z0-9]{4,32}$`) and `p_campaign_ref` (a slug) are required; otherwise `invalid_request`.
- The offer is the `active` catalog row with the same tenant, scope, scope version, product and funnel as the default offer, and `offer_code = p_offer_code` (highest `version`). If there is none: `blocked_campaign_offer`, and nothing is written.
- The lead's intent does not replace the offer of the campaign; its precheckout submission still provides the `sck` and the `fbclid`.
- Replay: the same message returns the same issuance only if it was reserved with the same offer, as `campaign`, for the same campaign; otherwise `replay_conflict`.
- Authorization, finalization and purchase correlation do not change: they look at the case, the scope and the `sck`, not at the offer.

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

The category is whatever Meta assigned; `UTILITY` is the intent.

**With a template name, the link is always its own message.** Since E-292 (Dan's condition of 2026-11-02, decision P-07 point 4: the link goes as an "independent message"), the reply goes out as one batch of `n + 1` parts with the existing per-part idempotency:
1. Parts `1..n` are the agent's text, cut by the reply splitter exactly as in any other turn (`CHATWOOT_REPLY_SPLITTER_ENABLED`, with its per-conversation and per-trigger manifest). They carry no URL and no issuance authorization. The parts must rebuild the text exactly (`validate_reply_parts`) and be at most three, so the batch fits the four-part limit; otherwise the text goes as one part. With the splitter off, `n = 1`.
2. Part `n + 1` is the link: the template, posted with `template_params`: `name`, `category`, `language`, and `processed_params.buttons[0] = {type: url, parameter: <suffix>}`. There is no `body` key, because the body has no placeholders. If the template cannot be used, part `n + 1` is the URL alone (see the fallback below).

The configured reply-part delay (`CHATWOOT_REPLY_PART_DELAY_SECONDS`, 2 s by default) separates the parts, because Chatwoot sends each message to WhatsApp from its own job. A part that a retry finds already delivered (`duplicate`) is not waited on again.

**Authorization.** The durable issuance is authorized at part `n + 1`, immediately before its POST, and finalized with part `n + 1`'s Chatwoot message id. Retry semantics for the URL are unchanged. Because the batch has `n + 1` parts with the template or without it, a retry that switches from the template to the written URL reuses the same part identities and does not repeat any text bubble.

**What the team sees.** Part `n + 1`'s Chatwoot `content` is the template body, a newline, and the full URL. Chatwoot sends only the template to WhatsApp and shows `content` to the team. Readers that detect an agent link by `pay.hotmart.com` keep working, such as the coupon follow-up and the OS readers.

**Fallback to the written URL.** Part `n + 1` is the URL alone, as its own message after the agent's text, never appended to it, in any of these cases:
- the template is missing or not approved;
- the template fails validation;
- the catalog read fails;
- the URL does not fit the button suffix, which must match `[A-Za-z0-9_-]+\?[A-Za-z0-9._~%=&-]+` and be at most 1,800 characters.

In each case the bridge logs `payment_link_template_unavailable reason=<code>` at WARNING. The log line carries no URL.

**The link blocked by the live checks.** Before each part, the live checks can block the delivery: a takeover, a pause, or the conversation having advanced past the trigger. A blocked delivery is `blocked`, the issuance stays reserved and unsent, and the lead may have received part of the agent's text without the URL. What happens next depends on why (E-292):
- **The lead wrote after the trigger** (`conversation_advanced`, and a re-read of the Chatwoot history shows a public contact message after the trigger): the turn ends like a normal superseded turn. There is no handoff and no `automation_paused`. The new message has its own turn, where the agent answers it and sees that the link did not go out. The bridge logs `payment_link_superseded reason=contact_wrote_after_trigger issuance_id=<id>`.
- **Any other reason**, or the history cannot be re-read: the conversation goes to a human with `payment_link_<reason>`, as before.

**Not handled.** If WhatsApp fails the template after Chatwoot accepted it, the message turns `failed` in Chatwoot, and the bridge does not watch for that.

## Purchase correlation

For `PURCHASE_APPROVED`, Hotmart supplies SCK at `data.purchase.origin.sck`. The parser preserves it exactly. A value is eligible when the hermes marker closes it: either `hermes|v1|<valid-ulid>` or `<ad sck>|hermes|v1|<valid-ulid>`. The marker must be the suffix, so a lead supplied value cannot impersonate the tail the reporting reads. Exact lookup on the stored SCK marks the issuance and linked intent purchased. Missing, malformed, unknown, or conflicting values do not guess by conversation or buyer identity.

Correlation proves which issuance produced the sale, not that the original contact was the final buyer of a shared link.

## Safety and rollout

Immediate opt-out, known purchase, takeover/handoff, pause, replaced sequence, identity mismatch, conversation mismatch, stale trigger, invalid catalog, or persistence failure blocks before POST. URLs must not enter logs, errors, screenshots, or broad audit output.

Migration, deployment, enablement, live sending, and Hotmart purchase testing are separate operations. Final acceptance requires a controlled purchase whose Hotmart report returns the exact SCK.
