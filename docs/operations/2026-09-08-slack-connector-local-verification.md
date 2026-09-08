# Verificación local del conector central de Slack

- **Fecha:** 2026-09-08
- **Estado:** implementación local verificada; despliegue y activación pendientes
- **Alcance:** conector outbound central, ledger durable, autenticación interna, productor y empaquetado

## Evidencia

### Suite focalizada

```text
uv run pytest tests/test_slack_connector_app.py tests/test_slack_connector_store.py tests/test_slack_connector_http_e2e.py tests/test_slack_connector_producer.py tests/test_slack_connector_worker_halt.py tests/test_slack_connector_readiness.py tests/test_slack_connector_capacity.py tests/test_bridge_slack_notifications.py tests/test_slack_correlation_ui.py -q
........................................................ [100%]
```

Resultado: 56 pruebas pasaron.

### Suite completa

```text
uv run pytest -q
[100%]
exit_code=0
```

Resultado: suite completa verde en una sola ejecución posterior a las correcciones finales.

### HTTP real local

`tests/test_slack_connector_http_e2e.py` levantó dos servidores TCP reales:

1. Uvicorn con el conector;
2. un servidor HTTP Slack sintético en loopback.

La prueba verificó `auth.test`, admisión autenticada, publicación, canal fijo,
contenido sanitizado, estado durable `accepted` y ausencia de un segundo mensaje
ante replay.

### Fallo seguro y recuperación

Se verificó:

- `503` durable cuando el storage deja de estar disponible;
- liberación del claim si falla la transición previa al request;
- `delivery_unknown` sin retry si el resultado de Slack es incierto;
- detención de la cola ante fallo de proveedor;
- recuperación después de una nueva verificación `auth.test` exitosa;
- aislamiento de tokens y lecturas entre `johanna` y `att1`;
- bloqueo de una segunda instancia sobre el mismo volumen;
- límite durable de pendientes y HTTP `429` retryable;
- thread keys construidas con el UUID completo;
- timestamps normalizados a UTC `Z`.

### Empaquetado

```text
uv build --out-dir /tmp/slack-connector-build
Successfully built ai_appointment_setter-0.1.0.tar.gz
Successfully built ai_appointment_setter-0.1.0-py3-none-any.whl
```

`git diff --check`, `python -m compileall` y el preflight multiagente pasaron.

## Limitación del entorno de verificación

No se pudo ejecutar `docker build` porque el host no tiene un daemon Docker
activo. El Dockerfile queda pendiente de su build real en EasyPanel/CI. No se
sustituye esa evidencia con una afirmación de imagen validada.

## Efectos externos

- mensajes reales enviados a Slack: **0**;
- secretos instalados: **0**;
- servicio EasyPanel creado: **no**;
- deploy ejecutado: **no**.

La primera publicación debe seguir siendo sintética y controlada conforme a
`slack-connector-deployment-runbook.md`.
