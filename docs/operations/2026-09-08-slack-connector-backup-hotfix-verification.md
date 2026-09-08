# Verificación del hotfix de backup pre-activación Slack

- **Fecha:** 2026-09-08
- **Base:** `509ec5836634074746cf8b0f4497344225302564`
- **Estado:** implementado y verificado; integración pendiente
- **Efectos externos:** cero

## Corrección

El store permite construir un estado `request_started` antes de inicializar una generación de activación. El validador de backup ahora acepta `activation_generation_started=NULL` únicamente para ese estado cuando `activation_initialized=0`; con activación inicializada sigue exigiendo una generación durable.

## Evidencia

- RED: `initialize → admit → claim_next → mark_request_started → backup_to` devolvía `RuntimeError: invalid_backup`.
- GREEN focal: backup, store, activación, reconciliación y capacidad → `45 passed`.
- Suite completa: exit code `0`; `1598` pruebas recopiladas.
- Revisión independiente: `APPROVE`; confirmó aceptación sólo cuando la activación está sin inicializar y rechazo del mismo NULL después de inicializarla.
- `git diff --check` y preflight: exit code `0`.
- Único warning: deprecación preexistente Starlette/httpx.
