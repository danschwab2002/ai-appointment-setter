# Evidencia controlada: contrato Hotmart `PURCHASE_CANCELED`

- **Fecha:** 2026-08-27
- **Estado:** implementación local verificada; migración no aplicada; código no desplegado
- **Alcance:** elegibilidad de recuperación de compra cancelada

## Contrato observado y aceptado

El capture real sanitizado de Hotmart v2 confirmó:

```text
event = PURCHASE_CANCELED
version = 2.0.0
data.purchase.status = CANCELED
```

`data.purchase.payment.refusal_reason` puede contener texto específico del
procesador o estar ausente. Se conserva como evidencia cuando existe, pero no es
un gate de elegibilidad.

Permanecen sin cambios los gates de autenticación, versión, producto, oferta,
identidad, correlación, consentimiento, presupuesto compartido, opt-out, takeover,
idempotencia y precedencia de compra aprobada.

## Evidencia ejecutable

TDD observó RED antes de implementar:

- el parser rechazaba `CANCELED` con motivo libre;
- el parser rechazaba un evento sin motivo;
- la RPC devolvía `invalid_johanna_payment_failure_payload` para el contrato real;
- la columna y la RPC bloqueaban el motivo ausente.

Verificación final:

```text
uv run pytest
1145 passed

cd tests/sql/followup_engine && npm test
PASS; JOHANNA_PAYMENT_FAILURE_DURABLE_REVIEW_OK

schema inventory después de baseline + todas las migraciones
20260827000100: fingerprint_present (4/4)
```

Un probe HTTP real por loopback levantó el ASGI de la rama y envió un payload
autenticado con `PURCHASE_CANCELED + CANCELED` sin `refusal_reason`, usando
autoridad durable stateful y sender sin red:

```text
HTTP: 202
admisiones: 1
reservas: 1
sender calls: 1
finalizaciones: 1
llamadas externas: 0
```

Este probe demuestra transporte HTTP local, parsing, orquestación y efecto sobre
dobles stateful. No demuestra aplicación en Supabase Cloud, deploy productivo,
aceptación de Chatwoot ni entrega física de WhatsApp.

## Artefacto de migración

```text
supabase/migrations/20260827000100_hotmart_canceled_any_reason.sql
```

La migración preserva `CANCELLED` únicamente en la restricción de tabla para no
invalidar evidencia histórica. Toda admisión nueva y toda autorización de envío
exigen el estado proveedor observado `CANCELED`.
