# 2026-09-22 — Migración `20260922000100` aplicada: el link de pago lleva la oferta que vio el lead

Evidencia de la aplicación en Supabase productivo de la migración
`20260922000100_johanna_checkout_offer_by_lead_intent_v1.sql` (PR #167, merge
commit `db8dc6d`). Contrato: `docs/contracts/johanna-payment-link-v2.md`.

## Commit fijado

- Merge remoto en `main`: `db8dc6d` (2026-09-22T13:36:36Z, merge commit de `4254ab9` + `38115ef`), verificado por ancestría contra `origin/main`.
- SHA-256 de la migración aplicada, igual en `origin/main` y en la copia usada para el apply: `e0df85552740839528fedd874a8610a5bb8e5ff1cda582b99e5a057ff53e4b3c`.
- Supabase CLI fijada: `2.113.0` (`npx --yes supabase@2.113.0`).

## Preflight (2026-09-22T13:41Z)

- Tracking remoto terminal antes del apply: `20260917000100_operator_correlation_abstention_reason`.
- `db push --linked --dry-run`: exactamente una migración pendiente, `20260922000100_johanna_checkout_offer_by_lead_intent_v1.sql`.
- Catálogo antes del apply: 1 fila (`ads-a` / `bxjge6zq`, default).
- Emisiones existentes: 1 (`accepted_by_chatwoot`, conversación de prueba del 22/09 11:50Z).
- Runtime: bridge en servicio con `PAYMENT_LINK_ENABLED=true`. La migración se aplicó **sin pausar** ingreso ni workers: reemplaza una función con el mismo nombre y firma, agrega dos columnas con default e inserta cinco filas, en una transacción con `lock_timeout = 5s`. Decisión de la sesión; el runbook del primer release pedía quiescencia para un stack completo.

## Apply

- Ejecutado desde el contenedor de Hermes (`infra_hermes`, usuario `hermes`), sobre una copia temporal de `supabase/` en `db8dc6d`. Motivo de la copia: `supabase/.temp/` (estado de `link`) no está en `.gitignore` y ensuciaría el clon canónico.
- La CLI conectó con `SUPABASE_ACCESS_TOKEN` + `SUPABASE_PROJECT_REF` ("Initialising login role..."); no pidió contraseña de la base.
- `db push --linked --yes`: `Applying migration 20260922000100_johanna_checkout_offer_by_lead_intent_v1.sql... Finished supabase db push.` Inicio 2026-09-22T13:41:27Z, fin 2026-09-22T13:41:37Z, `exit_code=0`.
- `migration list --linked`: `20260922000100` presente en local y remoto.
- `db push --linked --dry-run` posterior: `Remote database is up to date`.
- No se imprimieron ni persistieron credenciales.

## Postflight (Management API, solo lectura, 2026-09-22T13:46Z)

- `scripts/supabase_schema_inventory.sql` contra producción: 74 versiones, todas `fingerprint_present`; `20260914000100` 5/5 y `20260922000100` 5/5.
- `checkout_offer_catalog` (tenant `lancemos`, scope `libre-de-ansiedad-inbound`): 6 filas activas, una sola default:

  | landing | oferta | default | approved_by | approved_at |
  |---|---|---|---|---|
  | ads-a | bxjge6zq | sí | dan-schwab | 2026-09-14T22:11:14Z |
  | ads-b | mgbgpp19 | no | dan-schwab | 2026-09-22T12:30:00Z |
  | ads-c | s1qfxm7m | no | dan-schwab | 2026-09-22T12:30:00Z |
  | org-a | jtt6fcsm | no | dan-schwab | 2026-09-22T12:30:00Z |
  | org-b | ecyu87q0 | no | dan-schwab | 2026-09-22T12:30:00Z |
  | org-c | ulhzpw9a | no | dan-schwab | 2026-09-22T12:30:00Z |

- `checkout_link_issuances` por `offer_resolution`: `catalog_default` 1 (la fila anterior a la migración), ninguna otra todavía.
- `supabase_migrations.schema_migrations`: última `20260922000100`.
- Bridge: mismo contenedor, sin reinicio, `/ready` responde.

## Dato real que fija las ofertas

Sonda con navegador real sobre `pay.hotmart.com/F106691755G` (2026-09-22 ~13:40Z, pixel y GTM bloqueados, vista desde Argentina): las seis ofertas del catálogo rinden el mismo checkout del producto a ARS 85.624 con cuenta regresiva. `mgbqpp19`, el código que traen los eventos reales de abandono desde el 03/09, rinde ARS 139.794 sin cuenta regresiva, igual que un código inválido (`zzzzzzzz`): es la oferta default del producto (links de afiliado), no un error de tipeo de `mgbgpp19`. Sin `off`, Hotmart responde `error?errorMessage=008`. Un `curl` devuelve `200` para cualquier `off` y no sirve para validar ofertas.

## Escalón

**Activado**, no E2E. El comportamiento nuevo rige para toda emisión posterior a 2026-09-22T13:41:37Z sin redeploy. Queda pendiente el E2E: un lead que llegó por una landing distinta de `ads-a` pide el link y la URL emitida trae la oferta de esa landing (`offer_resolution = lead_intent`). `docs/current-state.md` se actualiza con ese dato.
