# Activación selectiva precheckout — 2026-08-30

- **Estado:** Evidencia operativa remota; baseline previo a la activación selectiva
- **Alcance:** bridge productivo y autoridad durable del first-touch de 60 minutos
- **No acredita:** aprobación Meta, POST outbound ni entrega WABA

## Baseline remoto

Consulta sanitaria ejecutada desde el task productivo con su identidad
`service_role`, sin exponer credenciales ni PII:

```text
migration_tracking_complete=true
scope_configured=true
runtime_state=inactive
runtime_generation=0
timer_binding_enabled=true
timer_binding_generation=2
first_touch_binding_enabled=false
due_count=0
reserved_count=0
request_started_count=0
delivery_unknown_count=0
reason_code=first_touch_binding_disabled
```

El ledger de migraciones incluye `20260829000200`–`20260829000500` según la RPC
service-role desplegada. La coordenada runtime `inactive/generation=0` es la forma
exigida por las funciones SQL V1; el binding first-touch es el interruptor operativo
de esta fuente.

## Bridge observado

```text
image=easypanel/infra/appointment-bridge:39d386fc2bd8ed0db2acd5465e08a57f87b1bd5f
health_http=200
ready_http=200
lead_precheckout_enabled=true
lead_precheckout_secret_present=true
hotmart_abandonment_timer_worker_enabled=false
precheckout_delayed_first_touch_enabled=absent
precheckout_delayed_outbound_enabled=absent
```

La ausencia de los dos flags delayed usa el default `false` de la revisión
observada. El ingreso real es `/webhooks/lead`, autenticado con HMAC; no se habilita
el receptor provisional `/webhooks/precheckout`.

## Frontera de esta evidencia

Este baseline demuestra preparación Cloud y despliegue previo, pero todavía no la
activación selectiva. La evidencia posterior debe registrar una nueva imagen por
SHA, worker y first-touch activos, outbound final explícitamente `false`, readiness
HTTP 200 y delta cero en `request_started` durante la operación.
