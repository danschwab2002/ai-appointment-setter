# 2026-09-22 — Bridge deployed and migration 20260922000200 applied

Evidence for the change that makes the recuperador link carry the lead's
`fbclid` and keep the ad's SCK in front of the hermes marker.

Authorised by Dan on 2026-09-22, deploy first and migration second, in that
order on purpose: this change touches `src/bridge/` as well as SQL.

## Pinned commit

| Item | Value |
|---|---|
| Merge commit on `origin/main` | `180c81584daee63736398b9cf8412f5d4c5bf9b4` (PR #169) |
| Feature commit | `19cbe6b4addab8fd260a08b11bd66a1b45df2747` |
| Merged at | 2026-09-22T22:43:12Z, ancestry verified against `origin/main` |
| CI | `verify` SUCCESS |
| Migration | `20260922000200_johanna_checkout_link_full_attribution_v1.sql` |
| sha256 | `f6022cc5a5c2bda7be8253ef79b4c8b468138303c0316ceb2daa800f2e2c5de4`, identical in `origin/main` and in the copy that was pushed |

## Why the order matters

The migration alone would have broken the purchase correlation, which is the
mechanism that lets a recovered sale be counted. Two readers assumed the SCK
*starts* with `hermes|`:

1. `src/bridge/app.py` only called the correlation for such values. With the ad
   SCK in front, the webhook answered `503`, the error that makes a provider
   disable an integration.
2. `correlate_hotmart_checkout_issuance_v2` rejected any prefix up front with
   `invalid_hermes_sck`.

Both fixes ship together in `180c815`. Deploying the bridge first is safe
because the new reader accepts the old and the new format; applying the
migration first would have opened a window where every recovered sale was lost.

## Deploy of the bridge (verified, not assumed)

Container `infra_appointment-bridge.1.o68782evtfffdwxoz1bo99gdc`, started
`2026-09-22T22:51:27Z`, `restarts=0`, `health=healthy`, `/ready` → `ready`.

Checked inside the running container rather than against the deploy notice:

| Check | Result |
|---|---|
| `bridge.supabase.sck_carries_hermes_issuance` importable | yes |
| accepts `fb.paid.<id>\|hermes\|v1\|<ulid>` | `True` |
| accepts `hermes\|v1\|<ulid>` | `True` |
| rejects `hermes\|nada` | `False` |
| `app.py` calls the helper | yes |
| `app.py` no longer uses `origin_sck.startswith` | confirmed absent |
| checkout query keys admit `fbclid` | yes |

## Preflight

Canonical checkout at `180c815`, working tree clean (0 dirty files). Applied
from a temporary copy of `supabase/` because `link` writes `supabase/.temp/`,
which is not gitignored. Dry run before applying:

```
Would push these migrations:
 • 20260922000200_johanna_checkout_link_full_attribution_v1.sql
```

## Apply

| Field | Value |
|---|---|
| CLI | `npx --yes supabase@2.113.0 db push --linked --yes`, run as `hermes` inside `infra_hermes` |
| Started | 2026-09-22T22:55:17Z |
| Finished | 2026-09-22T22:55:36Z |
| Reported | `Applying migration 20260922000200_...` then `Finished supabase db push.` |
| Exit code | 0 |
| Local/remote tracking | `20260922000200` on both |
| Dry run afterwards | `Remote database is up to date.` |

Applied hot, without pausing intake or workers: the migration is small and
transactional, and the aim was not to interrupt a link that is already live.

## Postflight (Supabase Management API, read only)

| Check | Result |
|---|---|
| Schema inventory | 75 versions, **75 `fingerprint_present`**, none absent or partial |
| `20260922000200` fingerprint | present, 6/6, `johanna_checkout_link_full_attribution_service_role_only` |
| `20260922000100` fingerprint | still present, 5/5 |
| `attribution_resolution` | `text not null default 'marker_only'`, CHECK over the four values |
| `dropped_unsafe_fields` | `text` nullable |
| `checkout_link_issuances_sck_value_shape` | present |
| `checkout_link_issuances_url_shape` | present, admits the composite SCK and the optional `fbclid` |
| Anonymous marker constraint | replaced; the `do $constraint$` block found it by definition, as verified beforehand |
| `reserve_chatwoot_checkout_issuance_v2` | security definer, contains `v_lead_fbclid`, EXECUTE for `service_role` only |
| `correlate_hotmart_checkout_issuance_v2` | security definer, accepts the optional prefix, no `service_role` grant (unchanged) |
| `anon` / `authenticated` | no EXECUTE on any of the three |
| Existing rows | 2, both `marker_only` with no dropped fields, which is correct: their URLs only carried the marker |

Bridge after the migration: same container, `restarts=0`, `health=healthy`,
`/ready` → `ready`. The RPC keeps its name and signature, so no redeploy was
needed for the migration itself.

## Step reached

**Activated.** Merged, bridge deployed, migration applied, behaviour live from
`2026-09-22T22:55:36Z`.

**Not yet E2E.** That needs a lead who arrived with an advertising SCK or a
`fbclid` to ask for the link, and the resulting URL to carry them. The check is
`attribution_resolution` on the new issuance: anything other than `marker_only`
proves the path. `docs/current-state.md` is written then, not now.

⚠ One thing that blocks the E2E in practice: while a human is handling a
conversation the bridge hands off instead of sending the link. That is what
happened on conversation 138 earlier today, where the offer resolution worked
(`lead_intent`, `off=mgbgpp19`) but the issuance stayed `reserved` and the link
never went out.
