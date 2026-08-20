# Correlación Hotmart ↔ purchase intent: Cloud y E2E controlado

- **Fecha:** 2026-08-20
- **Tipo:** evidencia operativa sanitizada
- **Estado:** implementado y verificado en Supabase Cloud y Appointment Bridge
- **Outbound:** desactivado durante toda la corrida

## Alcance y procedencia

Esta evidencia demuestra el circuito productivo del bridge con una reproducción manual autenticada de payloads oficiales Hotmart v2.0.0. **No demuestra entrega originada por la plataforma Hotmart**. Los eventos se enviaron desde el task productivo contra `localhost`, usando el Hottok ya configurado en el entorno; el secreto no salió del contenedor ni se registró.

La identidad fue sintética. No se registran email, teléfono, Hottok, firmas, tokens ni payloads completos.

## Release desplegado

```text
PR correlación expand:            #50
PR search_path owner-only bases:  #51
PR confirmed_abandonment:         #52
runtime source/image commit:       7a90269bca9cd1fd46f7bde27219c69f4f18fbdd
runtime image ID:                  sha256:6901296c58d29a161b5a65dbe7000f573def8208594bdd632f5e2ac381a7d38c
schema/docs merge commit:          d5bd82f9984ad087eca1230c431d43d6ee9d9fe5
```

Migraciones Cloud aplicadas y trackeadas:

```text
20260820000100_hotmart_purchase_intent_correlation
20260820000200_hotmart_intent_base_search_path
20260820000300_hotmart_confirmed_abandonment
```

Postflight de catálogo:

```text
correlator SECURITY DEFINER: true
correlator search_path: pg_catalog, public, pg_temp
service_role execute: true
anon/authenticated execute: false
owner-only admission bases direct API execute: false
constraint final contiene confirmed_abandonment: true
constraint final contiene abandonment_candidate: false
```

## Gates previos

```text
pytest: 949 PASS
PostgreSQL 17: 28 migraciones PASS
confirmed-abandonment backfill: PASS
resolved/unmatched/ambiguous/conflict: PASS
purchase supersedes abandonment: PASS
rolling expand shims: PASS
Python/SQL identity parity: PASS
PGlite/ACL: 89 funciones, 35 entrypoints, sin leaks
pglast/build/preflight/schema fingerprints: PASS
revisión independiente: APPROVE
GitHub required check verify: PASS
```

## Configuración segura observada

```text
LEAD_PRECHECKOUT_ENABLED=true
PRECHECKOUT_FIRST_TOUCH_ENABLED=false/unset
HOTMART_PURCHASE_WORKER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
marketing_optin=false
```

El endpoint Hotmart rechazó una solicitud sin Hottok con HTTP 401. `/health` y `/ready` respondieron HTTP 200; `automation_state=default_off`.

## Backfill del primer evento correlacionado

El primer evento controlado `PURCHASE_OUT_OF_SHOPPING_CART` fue admitido y correlacionado antes de `20260820000300`. Había producido el nombre histórico `abandonment_candidate`. El postflight posterior a la migración confirmó sobre la misma intención:

```text
current_classification=confirmed_abandonment
lifecycle_state=waiting_for_purchase
activation_authorized=false
whatsapp_contact_authorized=false
scheduled_actions=9
followup_delivery_attempts=9
```

Los conteos `9/9` eran preexistentes y no cambiaron.

## Corrida final aceptada

Una corrida intermedia se descartó como evidencia de intención fresca porque el fixture reutilizó un teléfono sintético anterior. Los eventos quedaron durables y sin efectos, pero no se usan para acreditar unicidad.

La corrida aceptada usó email y teléfono sintéticos nuevos. IDs opacos:

```text
delivery_id=01799TG12C6HZGYVR057B2H5WY
cart_event_id=e2e-cart-acdd72c60721b7ee2d2a
purchase_event_id=e2e-purchase-7ff318aef1927a742b03
purchase_intent_id=5b3b5d70-82a0-4637-82ff-9ef171a49a5f
```

Recepción HTTP:

```text
POST /webhooks/lead:    200 received
POST /webhooks/hotmart PURCHASE_OUT_OF_SHOPPING_CART: 202 received
POST /webhooks/hotmart PURCHASE_APPROVED:             202 received
```

Ledger durable de ambos eventos:

```text
PURCHASE_OUT_OF_SHOPPING_CART
  outcome=resolved
  matched_by=email_and_phone
  candidate_count=1
  reason_code=exact_email_and_phone
  manual_handoff_required=false

PURCHASE_APPROVED
  outcome=resolved
  matched_by=email_and_phone
  candidate_count=1
  reason_code=exact_email_and_phone
  manual_handoff_required=false
```

Estado final de la intención luego de la compra:

```text
lifecycle_state=purchased
current_classification=null
activation_authorized=false
whatsapp_contact_authorized=false
provider_observed=true
provisional=false
```

Conteos después de la corrida:

```text
hotmart_purchase_intent_correlations=5
hotmart_purchase_intent_correlation_candidates=5
scheduled_actions=9
followup_delivery_attempts=9
```

Las cinco correlaciones/candidatos incluyen las corridas controladas de diagnóstico y la corrida final. Las acciones e intentos de entrega no aumentaron. No se habilitaron workers, dispatcher, WhatsApp, email ni follow-ups.

## Resultado

El corte expand quedó demostrado para réplicas nuevas y compatibilidad temporal legacy:

```text
lead.precheckout durable
→ intención waiting_for_purchase sin autorización
→ abandono Hotmart autoritativo resolved
→ confirmed_abandonment sin autorización ni efectos
→ compra Hotmart autoritativa resolved
→ purchased y recuperación bloqueada
→ cero outbound
```

Los shims legacy permanecen disponibles sólo para la fase expand. Su revocación corresponde a una migración contract posterior, después de confirmar que no quedan réplicas viejas.
