# Verificación local reproducible — `commercial_cases` Corte A

- **Fecha:** 2026-08-16
- **Alcance:** implementación local del Corte A definido en ADR-0013
- **Estado:** verificación local e independiente aprobada; no desplegado
- **No demuestra:** aplicación en PostgreSQL real, Supabase Cloud, postflight remoto ni activación runtime

## Artefacto verificado

- Archivo: `/opt/data/cache/commercial-case-cut-a-complete-delta.tar.gz`
- SHA-256: `98eca931d1233f3f6e8be780a10d9b1d65346f546da04ed1f5ace1a8c0dd31cb`
- Contenido: 32 archivos del delta completo del worktree
- Baseline usado por la revisión independiente: `b93f2f445e9e08c56fa043738b75e47b566ccdec`

La revisión independiente aplicó exactamente el tarball sobre un clon limpio. El conjunto de archivos modificados/no trackeados resultante coincidió uno-a-uno con las 32 entradas del artefacto.

## Gates ejecutados

- `uv run pytest -q`: PASS en el worktree y en el clon de reproducción.
- `npm test` en `tests/sql/followup_engine`: PASS en el worktree y en el clon después de instalar las dependencias declaradas con `npm ci`.
- `git diff --check`: PASS.
- `uv run python scripts/agent_workspace.py preflight`: PASS en el worktree.

## Invariantes verificadas

- backfill uno-a-uno de recoveries existentes;
- creación y sincronización de la sombra para recoveries nuevos;
- constraints inmediatos;
- update-delete e insert-delete en una misma transacción;
- ausencia global de recoveries o raíces huérfanas;
- rechazo de mutación y delete directo o anidado de la sombra;
- preservación de `ON DELETE SET NULL` para conversación e identidad;
- timestamps autoritativos sincronizados desde recovery;
- `inbound_sales` y `payment_failure` bloqueados;
- DML directo de `service_role` sobre `commercial_cases` denegado;
- write histórico de recovery bajo `service_role` permitido y sincronizado;
- exactamente dos funciones trigger `SECURITY DEFINER` con `search_path` endurecido;
- exactamente dos funciones trigger `SECURITY INVOKER`;
- cero privilegios `EXECUTE` efectivos para roles API sobre las cuatro funciones.

## Límite operativo

No se ejecutó migración en Supabase Cloud ni PostgreSQL externo. No se cambió autoridad runtime, handoff, scheduling, dispatcher u outbound. No hubo commit, push ni deploy como parte de esta verificación.
