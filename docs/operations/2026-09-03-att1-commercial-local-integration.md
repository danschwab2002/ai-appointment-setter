# Evidencia local: integración comercial ATT1

- Fecha: 2026-09-03
- Rama local: `integration/att1-commercial-20260902`
- Estado: integración local verificada; no publicada ni desplegada
- Base remota observada: `origin/main` en `6cf0a2bbf273b2111682f08d2e8e2cbf738df24c`
- Commit fuente comercial integrado: `4176c8b94e28542cf6ca9c71e413544df43c6df7`

## Resultado

La rama de integración avanzó por `fast-forward` desde la base remota hasta el
commit comercial revisado. Su historia contiene, en orden:

- `70bb0135b245b80958ebead5f863198bb908dd40`: runtime ATT1 portable;
- `2b632126de6dee20791769d7baa17c88af0f0023`: cierre de información comercial;
- `4176c8b94e28542cf6ca9c71e413544df43c6df7`: decisión de descuento ATT1.

No hubo conflictos ni resoluciones manuales. La integración conserva los gates
`conversation_release_approved=false` y `activation_authorized=false`; tampoco
publica una política de descuento.

## Verificación combinada

Pasaron con código 0:

- `uv run pytest -q`;
- `npm ci && npm test` en `tests/sql/followup_engine`;
- `python3 /opt/data/cache/att1_postgres17_probe.py` contra PostgreSQL real
  rootless 17.11, aplicando 53 migraciones;
- `python -m compileall`, parseo de todos los JSON bajo `config/`, `node --check`,
  `git diff --check` y el preflight de coordinación.

La primera ejecución de Python, lanzada en paralelo con los otros harnesses,
falló únicamente en el test de timing conocido
`test_handoff_worker_stop_is_bounded_during_stuck_finalization`. La ejecución
canónica repetida en aislamiento pasó completa sin cambios de producto. El
probe PostgreSQL requirió actualizar en su harness temporal externo a Git la
ruta del antiguo worktree; después pasó sin cambios en el repositorio.

## Prueba HTTP real local

Se inició la fábrica ASGI real mediante Uvicorn sobre TCP loopback y se
consultaron `/health` y `/ready`. Resultado sanitizado:

```text
real_http_probe=OK
health_status=ok
ready_status=ready
pilot_boundary=disabled
automation_state=default_off
server_stopped=true
```

Esto prueba arranque, lifespan y readiness del commit integrado. No prueba
Supabase Cloud ni conexiones o entregas físicas de Hotmart, Chatwoot o WABA.

## Fronteras no cruzadas

- sin push, PR o merge remoto;
- sin migración Supabase Cloud;
- sin deploy ni cambios en EasyPanel;
- sin publicación de Conversation Release o política de descuento;
- sin creación o activación del profile runtime ATT1;
- sin mensajes reales;
- sin secretos ni PII incorporados.
