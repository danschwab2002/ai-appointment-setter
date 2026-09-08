# Verificación local del hardening del conector Slack

- **Estado:** Implementado y verificado localmente
- **Fecha:** 2026-09-08
- **Branch:** `feat/slack-connector-operational-hardening-v1`
- **Base:** `4e91f9a9f1d2dc7e6a555a43b26f448ee763bfad`
- **No realizado:** despliegue o efecto Slack real

## Evidencia ejecutada

- RED inicial de activation/reconciliation/backup: `10 failed` por APIs aún ausentes.
- RED de bloqueo por incertidumbre: `test_new_activation_generation_is_blocked_by_delivery_unknown` falló porque no rechazaba el avance; luego quedó verde.
- Suite focal final:
  `uv run pytest -q tests/test_slack_connector_activation.py tests/test_slack_connector_reconciliation.py tests/test_slack_connector_backup.py tests/test_slack_connector_app.py tests/test_slack_connector_readiness.py tests/test_slack_connector_http_e2e.py`
  → `40 passed` (1 warning deprecado de Starlette/httpx).
- Suite Python completa: `uv run pytest -q` → exit `0`; la colección contiene `1587` pruebas (1 warning deprecado de Starlette/httpx).
- Configuración fail-closed: outbound rechaza `SLACK_ACTIVATION_MODE=inactive`; no existe activación implícita.
- Healthcheck de contenedor: `/health` sólo comprueba liveness; la promoción operativa exige verificar `/ready` por separado para evitar ciclos de reinicio ante caídas de Slack.
- Reinicio seguro: un `delivery_unknown` durable detiene el worker antes de reclamar trabajo posterior; ni `auth.test` ni un restart lo reanudan sin reconciliación.
- `git diff --check` → exit `0`.
- `uv run python -m compileall -q ...` → exit `0`.
- `uv run python scripts/agent_workspace.py validate-tree` → `migration_files=61`, `duplicate_migration_versions=0`.
- `uv run python scripts/agent_workspace.py preflight` → exit `0`, claim `implementing` correcto.
- HTTP TCP real con factory Uvicorn, efectos inactivos y storage preflight activo:
  - `/health` → `200 {"status":"ok"}`
  - `/ready` → `200`, `mode=inactive`, `storage_ready=true`, los cuatro conteos ledger en cero y activation inactive generation 0.
- CLI: `python -m slack_correlation.store --help` → exit `0`; el test focal ejecuta backup y restore reales y valida `integrity_check=ok`.

## Limitación del entorno

No se pudo construir la imagen: `docker build ...` devolvió `Cannot connect to the Docker daemon at unix:///var/run/docker.sock`. El Compose plugin tampoco está instalado (`docker compose` no reconocido). El Dockerfile sí quedó fijado al digest requerido y el YAML pasó el validador de escritura del repositorio.
