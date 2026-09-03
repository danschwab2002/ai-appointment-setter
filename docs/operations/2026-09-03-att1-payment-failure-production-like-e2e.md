# Evidencia local: pago fallido ATT1 production-like

- Fecha: 2026-09-03
- Rama local: `feat/att1-production-like-final-meta-gate`
- Estado: implementación y E2E local verificados; no publicados ni desplegados
- Efecto externo: bloqueado por `FinalMetaEffectGate`

## Alcance verificado

El escenario envió un `PURCHASE_CANCELED` versión `2.0.0` por HTTP real sobre
TCP loopback a la fábrica ASGI ejecutada con Uvicorn y lifespan habilitado. El
request usó el header Hotmart real y un payload sintético compatible con el
binding ATT1.

Después de la admisión, el escenario ejecutó las implementaciones reales de
`ResolutionWorker` y `DurableDispatcher` contra una autoridad stateful de prueba.
Verificó:

- admisión portable del evento de pago fallido;
- normalización y resolución de identidad;
- planificación específica de pago fallido;
- ausencia de autorización implícita creada por el evento de pago fallido;
- acción durable con `anchor_type=payment_failure`;
- composición del template `att1_compra_fallida_01`;
- reevaluación y reserva previas al efecto;
- autorización explícita de formulario en el fixture positivo;
- cierre del gate Meta antes de `request_started` y antes del sender;
- evidencia `final_meta_gate_closed` y resultado `final_effect_blocked`;
- cero llamadas al sender y cero llamadas a la autoridad de request-start;
- ausencia de finalización falsa como aceptado, enviado o entregado.

## Verificación ejecutada

Pasaron con código 0:

- `uv run pytest -q`;
- `npm test --prefix tests/sql/followup_engine`;
- `uv run pytest tests/e2e/test_att1_production_like_final_gate.py -q --tb=short`.

La suite SQL agregada incluyó
`validate_commercial_ally_payment_failure_recovery.mjs` y produjo
`commercial_ally_payment_failure_recovery=OK`. El control ACL produjo
`acl_hardening=OK` con 62 entrypoints de servicio inventariados. La suite Python
sólo emitió el warning de deprecación ya existente entre FastAPI TestClient y
Starlette.

## Límites de esta evidencia

La prueba HTTP acredita TCP loopback, arranque ASGI/lifespan, autenticación y
routing Hotmart, además de la orquestación real de resolver y dispatcher. La
autoridad durable fue un double stateful dentro del proceso: esta evidencia no
acredita persistencia tras reinicio, PostgREST ni DDL aplicado en Supabase Cloud.
Esas semánticas se verificaron separadamente con la suite SQL local, no con una
base gestionada.

No se llamó a Hotmart, Chatwoot ni Meta reales. Cero llamadas al sender inyectado
no constituye una medición de servicios remotos. Tampoco acredita deploy,
credenciales, aprobación del template en Meta ni entrega física.

## Fronteras no cruzadas

- sin commit, push, PR o merge;
- sin migraciones remotas;
- sin deploy ni cambios en EasyPanel;
- sin habilitar el request HTTP final a Meta;
- sin mensajes reales;
- sin secretos ni PII reales en fixtures o evidencia.
