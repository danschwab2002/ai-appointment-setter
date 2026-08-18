# Pre-checkout receiver test-only E2E

- **Fecha:** 2026-08-18
- **Tipo:** evidencia operativa sanitizada
- **Commit desplegado:** `62e601dc7a157d17fb7c2855d3e6a7bac7f39046`
- **Estado:** receiver test-only activo; outbound y workers apagados
- **Alcance:** ingreso provisional emulado para el único JID allowlisted

## Activación controlada

Se generó un token aislado directamente en el host productivo y se configuró un teléfono de prueba derivado del `ALLOWED_WHATSAPP_JID` canónico. Ninguno de esos valores se registró en esta evidencia.

Flags efectivos:

```text
PRECHECKOUT_FORM_ENABLED=true
PRECHECKOUT_TEST_MODE_ENABLED=true
DURABLE_OUTBOUND_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
```

Readiness posterior:

```text
/ready: HTTP 200
status: ready
automation_state: paused
```

## Probes negativos

1. Token inválido con body no JSON:

```text
HTTP 401
```

Esto demuestra que el token se rechaza antes de parsear el body.

2. Token válido con E.164 distinto del JID allowlisted:

```text
HTTP 403
```

Postflight posterior:

```text
submissions: 0
purchase intents: 0
links: 0
conflicts: 0
unsafe true flags: 0
```

## Probe positivo e idempotencia

Referencia opaca:

```text
controlled-e2e-cded37c89226457c92b73d1d1bae369b
```

Resultados:

```text
primera admisión: HTTP 202, status=received
replay exacto: HTTP 200, status=duplicate
misma purchase_intent: true
test_only: true
generalizable: false
activation_authorized: false
```

Postflight durable:

```text
submissions: 1
purchase intents: 1
links: 1
conflicts: 0
lifecycle: waiting_for_purchase
product_ref: F106691755G
offer_ref: bxjge6zq
provisional: true
whatsapp_contact_authorized: false
provider_observed: false
activation_authorized: false
```

## Cero efectos

Después del ingreso y replay:

```text
eligible scheduled actions: 0
outbound request authorizations: 0
nonterminal Hotmart events: 0
nonpaused pilot runtimes: 0
```

No se inició sender, dispatcher, resolution worker ni purchase worker. No se envió ningún mensaje.

## Bloqueo para el primer outbound

La admisión termina deliberadamente en una intención no autorizada. Todavía no existe una transición implementada desde esta `purchase_intent` hacia un caso de recuperación autorizable.

Además, el inventario remoto conserva dos memberships históricas activas y un attempt histórico en fase `reserved`; antes de cualquier activación outbound deben clasificarse con sus acciones y scopes, preservar evidencia y demostrar que no son reclamables.

El próximo corte debe implementar una autorización test-only explícita y durable para una única intención, crear como máximo un first touch, exigir runtime/budget/cohorte de una sola persona y reautorizar inmediatamente antes del request-start. No puede inferir consentimiento desde el formulario ni activar follow-ups.
