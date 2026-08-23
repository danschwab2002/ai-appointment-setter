# Contrato V1 — wiring runtime del perímetro Lancemos

- **Estado:** Implementado en el árbol; no desplegado
- **Versión:** 1
- **Fecha:** 2026-08-10
- **Alcance:** planificación, request-start y readiness del piloto

## 1. Configuración del proceso

`LANCEMOS_PILOT_BOUNDARY_ENABLED` es `false` por defecto. Cuando vale `true`, el proceso exige al arrancar:

- `LANCEMOS_PILOT_SCOPE_KEY` no vacío;
- `LANCEMOS_PILOT_SCOPE_VERSION` entero positivo;
- `LANCEMOS_PILOT_TENANT_KEY` no vacío;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER` no vacío;
- `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF` no vacío.

`RESOLUTION_WORKER_ENABLED=true` y `DURABLE_OUTBOUND_ENABLED=true` requieren el perímetro habilitado. La falta de configuración impide iniciar la aplicación; no degrada al entrypoint histórico.

## 2. Planificación

La aplicación usa `plan_lancemos_pilot_cart_recovery` para abandono de carrito.

La RPC recibe el contrato existente de planificación, la identidad Chatwoot resuelta y sólo `scope_key/version`. Tenant y routing se derivan del scope publicado, no de afirmaciones del caller. En una transacción:

1. serializa contra cambios del runtime;
2. exige scope publicado y versión activa;
3. evalúa tenant, account/inbox, proveedor/cuenta, fuente/evento, producto, oferta y cohorte;
4. exige que policy key/version coincidan con el scope;
5. sólo entonces invoca la planificación durable autoritativa.
6. vincula el caso de forma inmutable en `pilot_recovery_case_bindings` con
   `scope_key/version` y el evento admitido.

Un rechazo usa SQLSTATE `55000`, mensaje `pilot_scope_rejected` y un `detail` reason code. La transacción no deja casos, secuencias ni acciones parciales.

Los RPC históricos de planificación no tienen `EXECUTE` para roles API después de esta migración.

## 3. Request-start

La aplicación usa `mark_lancemos_pilot_request_started` inmediatamente antes del sender.

La RPC no acepta scope, tenant ni routing. Deriva el binding inmutable del caso y, desde el estado canónico, contacto, producto, oferta, account e inbox. En una transacción:

1. ejecuta `authorize_lancemos_pilot_request_start`;
2. exige autorización actual para el mismo action/attempt;
3. compone los guards previos de autorización del contacto, compra, takeover y opt-out;
4. marca el intento como `request_started`;
5. devuelve el intento y la identidad durable de autorización.

Respuesta adicional obligatoria:

- `pilot_authorization_id: uuid`;
- `pilot_runtime_generation: bigint`;
- `pilot_authorization_replayed: boolean`.

El endpoint histórico `mark_followup_request_started` conserva su firma sólo para composición interna y falla con `pilot_request_authorization_required` si no existe autorización para el mismo action/attempt. No es ejecutable por roles API. La autorización standalone y las demás funciones internas tampoco son ejecutables por `service_role`, `anon` ni `authenticated`.

Un replay sólo es aceptable cuando el intento ya está en `request_started`. Una autorización huérfana falla con `pilot_authorization_without_request_start`.

## 4. Readiness operacional

- `GET /health`: liveness; responde `{"status":"ok"}` sin consultar dependencias.
- `GET /ready`: readiness sanitizada.

Con perímetro deshabilitado, `/ready` responde HTTP 200 y declara `default_off`.

Con perímetro habilitado, consulta `get_lancemos_pilot_runtime_status`. Un scope/version/tuple válido responde HTTP 200 incluso si el runtime está `inactive`, `paused` o `closed`: el proceso es desplegable aunque la automatización no esté armada. Configuración durable inconsistente o dependencia inaccesible responde HTTP 503.

La respuesta sólo expone:

- `status`;
- `pilot_boundary`;
- `automation_state`;
- `reason_code`.

No expone IDs de contacto, JID, teléfonos, emails, payloads, URLs ni credenciales. Los errores de dependencia se normalizan como `pilot_readiness_unavailable`.

## 5. Reason codes de readiness

- `pilot_boundary_disabled`;
- `pilot_runtime_config_invalid`;
- `pilot_scope_config_mismatch`;
- `pilot_active_scope_mismatch`;
- `pilot_runtime_inactive`;
- `pilot_runtime_armed`;
- `pilot_runtime_paused`;
- `pilot_runtime_closed`;
- `pilot_readiness_unavailable`.

## 6. Operación y compatibilidad

- La migración es aditiva, salvo el cierre explícito de los entrypoints históricos que permitían bypass.
- Con `channel_provider=waba`, outbound exige un template aprobado de primer
  contacto, idioma y categoría. Para el corte single-touch de carrito el body
  usa exactamente `{{1}} = nombre` y `{{2}} = oferta/producto`; un follow-up es
  opcional y, si no está configurado, se bloquea antes del POST. El bridge envía
  por el inbox WABA de Chatwoot usando `template_params` y nunca cae a texto libre.
- El dispatcher deriva el modo durable del provider: WABA reserva y audita
  `approved_template`; Evolution reserva y audita `freeform`.
- Request-start rechaza atómicamente `waba + freeform` y cualquier otra
  combinación provider/modo incompatible antes de crear autorización.
- La imagen y `compose.yaml` usan `/ready` como healthcheck.
- Todas las flags de efectos permanecen apagadas por defecto.
- Integrar este contrato no prueba migración aplicada, configuración remota, WABA disponible, runtime armado ni mensajes enviados.
