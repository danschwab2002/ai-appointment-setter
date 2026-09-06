# Evidencia local — planificación post-inbound ATT1

- **Fecha de cierre:** 2026-09-06T16:06:08Z
- **Estado:** implementación local verificada; no publicada, no desplegada y no aplicada a producción
- **Base:** `3b5bd1da2900b811fecefd495b6761ea4b48987c`
- **Alcance:** `payment_failure` → una acción `later_step` después de inbound válido

## Resultado durable

La migración `20260905000100_commercial_ally_post_inbound_discount.sql`:

- registra evidencia inbound sanitizada y append-only;
- crea como máximo una acción `inbound_reply_offer` por caso;
- conserva el 10 %, el cupón y la identidad de la política publicada;
- devuelve la misma acción ante replay o inbounds posteriores;
- conserva ese replay estable tras retiro de runtime/política, takeover o cierre
  del caso, sin reabrir ni autorizar la acción;
- rechaza un segundo tenant que comparta Account/Inbox pero no tenant, producto y offer del caso;
- deja la acción en `deferred` con `next_attempt_at = infinity` y `effect_authorized = false`;
- no crea timers ni acciones por silencio;
- expone sólo la RPC de planificación a `service_role`; la tabla y la función trigger no son ejecutables directamente por roles API.

Tras la admisión durable del webhook, el worker Chatwoot evalúa primero la RPC
especializada para una conversación de recuperación existente. Si crea o reutiliza
la acción —o determina que el caso no aplica— cierra ese inbound sin alta genérica
`inbound_sales`, Hermes ni sender. El gate no puede coexistir con el agente de Corte B.
Esto ocurre sólo para el tenant `att1` y con
`CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED=true`; el valor por defecto es `false`.

## Verificación

- `uv run pytest`: **1474 passed**, una advertencia de deprecación preexistente de `fastapi.testclient`.
- `npm test` en `tests/sql/followup_engine`: **exit 0**; incluye `commercial_ally_payment_failure_recovery=OK` y `acl_hardening=OK` con 63 entrypoints de servicio.
- Probe HTTP real con el `build_app` usado por el contenedor, Uvicorn sobre TCP loopback:
  - `GET /health`: HTTP 200, `status=ok`;
  - `GET /ready`: HTTP 200, `status=ready`, `automation_state=default_off`.
- `git diff --check`: sin errores.
- Preflight multiagente: exit 0.

La revisión adversarial del snapshot anterior detectó y motivó guards adicionales:
camino especializado antes de `inbound_sales`, tenant `att1`, Inbox ID canónico,
timestamp finito, UUIDs estrictos y `search_path` endurecido. Las suites y el probe
HTTP anteriores corresponden al candidato corregido. Una revisión posterior añadió
shape RPC exacto, conteo exacto del contacto inicial y cierre sin caída a Corte B.

## Frontera no cruzada

No se hizo commit, push, merge, despliegue, migración administrada ni mutación de Chatwoot/Meta. No se publicó política/cupón ni se autorizó el envío. El inbox/manifiesto ATT1 y la plantilla WABA aprobada continúan como gates externos para una activación posterior.