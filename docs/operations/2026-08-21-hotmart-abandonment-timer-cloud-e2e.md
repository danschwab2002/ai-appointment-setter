# Evidencia: timer Hotmart en Supabase Cloud y producción

- **Tipo:** Evidencia operativa sanitizada
- **Fecha:** 2026-08-21
- **Runtime verificado:** `0989f85e265f355514e95c90a579884ce99aad3e`
- **Scope:** `lancemos / psicologajohanna / F106691755G / bxjge6zq`
- **No prueba:** `lead.precheckout` V1.1.0 ni contacto real

## Precondiciones

```text
policy=lancemos-johanna-abandonment-reevaluation
version=1
status=published
delay=300 segundos
binding_enabled=true
binding_generation=1
matching_bindings=1
```

Durante las pruebas permanecieron apagados dispatcher, outbound, first touch,
purchase worker y resolution worker. El timer worker sólo se habilitó temporalmente
para consumir el timer controlado del primer escenario y volvió a `false`.

## Escenario A — abandono sin autorización

Dataset sintético nuevo y comprobado sin identidad histórica:

```text
lead.precheckout → HTTP 200
PURCHASE_OUT_OF_SHOPPING_CART → HTTP 202
correlation=resolved
matched_by=email_and_phone
candidate_count=1
intent.current_classification=confirmed_abandonment
timer.delay_seconds_snapshot=300
timer.status=scheduled
```

Después del vencimiento real:

```text
timer.status=completed
timer.outcome=blocked_not_authorized
terminal_transitions=1
pending_timers=0
```

Delta comercial:

```text
scheduled_actions:          9 → 9
followup_delivery_attempts: 9 → 9
messages:                  10 → 10
```

## Escenario B — compra antes del vencimiento

Dataset sintético independiente del escenario A:

```text
lead.precheckout → HTTP 200
PURCHASE_OUT_OF_SHOPPING_CART → HTTP 202
correlation=resolved
matched_by=email_and_phone
candidate_count=1
timer.status=scheduled
timer.delay_seconds_snapshot=300
```

La compra se admitió con margen antes de `due_at`:

```text
PURCHASE_APPROVED primera entrega → HTTP 202 / received
replay byte-idéntico              → HTTP 200 / duplicate
purchase_intent.lifecycle_state=purchased
timer.status=completed
timer.outcome=cancelled_purchased
completed_at < due_at
```

Idempotencia y delta:

```text
purchase events=1
purchase correlations=1
timers para el abandono=1
terminal transitions=1
semantic conflicts=0
pending timers=0
scheduled_actions:          9 → 9
followup_delivery_attempts: 9 → 9
messages:                  10 → 10
```

## Postflight

```text
/health=200 / ok
/ready=200 / ready
automation_state=default_off
errors=0
tracebacks=0
LEAD_PRECHECKOUT_ENABLED=true
HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
PRECHECKOUT_FIRST_TOUCH_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

No hubo llamadas a Hermes, Chatwoot, WhatsApp ni sender. Los scripts, datasets y
worktrees efímeros fueron eliminados al finalizar.

## Evidencia local posterior — contrato V1.1.0

La migración `20260821000200_lead_whatsapp_consent_authorization.sql` se aplicó en
una base PostgreSQL 17.10 vacía junto con las 31 migraciones canónicas. El probe
verificó localmente promoción `false|false → true|true`, mismatch fail-closed,
preservación durante abandono `resolved`, outcome
`blocked_contact_binding_missing` y delta comercial cero.

Esta última evidencia no implica merge, migración Cloud, despliegue ni activación
de la landing V1.1.0.
