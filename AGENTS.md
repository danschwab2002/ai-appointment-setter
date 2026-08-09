# Reglas del proyecto

## Alcance actual

Construir un receptor seguro para webhooks de Chatwoot antes de integrar el profile comercial de Hermes.

## Desarrollo multiagente obligatorio

- `main` es sólo para integración; ningún agente implementa directamente allí.
- Cada tarea sustancial requiere rama, worktree y claim exclusivos administrados por `scripts/agent_workspace.py`.
- Antes de editar y antes de commitear, ejecutar `uv run python scripts/agent_workspace.py preflight`; si falla, detenerse sin modificar archivos.
- Declarar paths y recursos semánticos del claim. No continuar ante solapamientos de scope, archivos reales o versiones de migración.
- Un worktree no reclamado es fail-closed: adoptarlo o crear uno nuevo antes de trabajar.
- Cada agente publica una rama limpia y la mueve a `review`; un único integrador procesa PRs de forma serial.
- No mergear ni desplegar desde un worktree sucio. El despliegue parte de un commit verificado de `origin/main`.
- Seguir el runbook `docs/operations/multi-agent-development-runbook.md` para crear, adoptar, revisar, integrar y limpiar tareas.

## Convenciones

- Python administrado con `uv`; no usar `pip` global.
- Desarrollo guiado por pruebas: ejecutar `uv run pytest`.
- Los secretos viven únicamente en `.env`, nunca en Git.
- Los payloads capturados y cualquier PII viven en `data/`, excluido de Git.
- No registrar tokens, firmas, API keys ni cuerpos completos en logs de aplicación.
- El agente comercial solo podrá actuar para el JID autorizado configurado.
- Antes de declarar una funcionalidad terminada, ejecutar pruebas y una verificación HTTP real.

## Documentación obligatoria

- Seguir `docs/documentation-governance.md` en toda tarea de diseño, arquitectura, contratos u operación.
- Documentar propuestas en `docs/design/`; crear ADR sólo para decisiones arquitectónicas aceptadas.
- Actualizar `docs/architecture.md` cuando cambie el sistema implementado y `docs/contracts/` cuando cambie una interfaz.
- Registrar en `docs/operations/` la evidencia operativa relevante, no diarios narrativos del proyecto.
- Mantener explícita la diferencia entre propuesta, decisión aceptada, implementación y evidencia.
- Evaluar y aplicar proactivamente las actualizaciones documentales correspondientes dentro de la misma tarea, sin tocar trabajo concurrente fuera de alcance.
