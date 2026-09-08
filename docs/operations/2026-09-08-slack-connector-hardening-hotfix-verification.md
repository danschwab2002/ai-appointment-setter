# Verificación local del hotfix de hardening Slack

- **Estado:** Implementado y verificado; revisión y merge pendientes
- **Fecha:** 2026-09-08
- **Branch:** `fix/slack-connector-hardening-hotfix-v1`
- **Base:** `243f560ce2a79a88b2cb3ed1b65cf61ced788947`
- **Efectos externos:** cero

## Correcciones

- El límite durable cuenta `delivery_unknown` junto con `pending`, `claimed` y `request_started`.
- Restore ya no confía sólo en `integrity_check` y `user_version=2`: obtiene una instantánea SQLite consistente con WAL y exige el inventario exacto de tablas/índices, columnas, claves, checks, foreign keys, singleton de activación y consistencia de filas, threads, auditoría y generaciones.
- El fence global de `delivery_unknown` incorporado antes del merge de PR #108 permanece cubierto.

## Evidencia

- RED reproducido: una entrega incierta con capacidad `1` permitió otra admisión.
- RED reproducido: una base SQLite vacía con `user_version=2` fue aceptada para restore.
- GREEN focal ampliado: capacidad, backup/restore, store, activación y reconciliación → `44 passed`.
- Suite completa: exit code `0`; `1597` pruebas recopiladas.
- `git diff --check`, `compileall`, `validate-tree` (`61` migraciones, `0` versiones duplicadas) y preflight: exit code `0`.
- Único warning: deprecación preexistente Starlette/httpx.
- Revisión independiente: pendiente de resultado final.
