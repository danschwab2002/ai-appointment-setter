# Evidencia local ATT1 UC-01: one-shot y bloqueo Meta

Fecha: 2026-09-04  
Estado: evidencia local; no acredita despliegue ni activación productiva.

## Alcance verificado

- Un caso de pago fallido conserva como máximo una acción `payment_failure_first_contact`, incluso si esa acción ya está terminal cuando ingresa otro `PURCHASE_CANCELED`.
- Un runtime con `tenant_ref=att1` rechaza el startup si `META_FINAL_EFFECT_ENABLED=true`.
- El E2E ATT1 usa Product ID `5071808`, hotlink `D98014973Y` y offer code observado `83utgyow`; no usa identificadores de otros productos.

## Evidencia

- `uv run pytest`: `1462 passed`, una advertencia de deprecación de Starlette.
- `node validate_commercial_ally_payment_failure_recovery.mjs`: `commercial_ally_payment_failure_recovery=OK`.
- `node validate_acl_hardening.mjs`: `acl_hardening=OK`; inventario de schema completo.
- Smoke ASGI real en loopback con manifiesto sintético y todos los efectos apagados:
  - `GET /health` → HTTP `200`, estado `ok`.
  - `GET /ready` → HTTP `503`, esperado porque no existe binding durable ATT1 provisionado para los IDs Chatwoot sintéticos.

## Límites y bloqueos

- La migración nueva no fue aplicada a Supabase Cloud ni a producción.
- No se desplegó una imagen nueva.
- Chatwoot ATT1, cuenta, inbox y usuario de Mariana no fueron creados: no existe acceso SSH utilizable ni destino SSH conocido.
- La rama respuesta inbound → cupón sigue bloqueada por ausencia de plantilla WABA aprobada y contrato exacto de componentes/variables.
- El envío final a Meta permanece físicamente bloqueado.
