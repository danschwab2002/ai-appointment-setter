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
- Los **contratos** (`docs/contracts/`) se escriben antes: son insumo de la construcción. Los **ADR** (`docs/decisions/`) se escriben cuando la decisión ya está aceptada.
- Una **propuesta** en `docs/design/` se escribe sólo cuando alguien tiene que revisarla antes de decidir. El commit y el PR ya llevan el razonamiento, con fecha y diff adjunto.
- El **estado del sistema** (`docs/current-state.md`, `docs/architecture.md`) y la **evidencia** (`docs/operations/`) se escriben después de verificar el efecto, con el número medido adentro.
- Mantener explícita la diferencia entre propuesta, decisión aceptada, implementación y evidencia.
- No tocar documentación de trabajo concurrente fuera de alcance.

## Verificación antes que ceremonia

Vigente desde el 2026-09-20. Reemplaza al protocolo de captura de aprendizaje de Johanna, que
exigía un documento narrativo por tarea y sólo se activaba cuando el claim mencionaba "johanna".

- **El dato real antes que el código.** Toda tarea empieza trayendo el payload o la respuesta real del borde que toca. Si el dato no se puede obtener, eso es la tarea: se reporta qué falta y dónde vive, y se detiene ahí. No se escribe código contra una suposición del payload.
- **Los bordes externos se prueban con fixtures capturados.** Todo test que simule Hotmart, Chatwoot, Evolution o Slack usa un payload capturado en `tests/fixtures/`, con su fecha y su origen anotados. Un payload inventado por quien escribe el test no prueba el borde.
- **Un cambio, un efecto observable.** Una suite en verde no es evidencia de efecto. Si medir el efecto exige un despliegue, la tarea queda explícitamente abierta y se reporta así.
- **Ningún documento afirma estado sin evidencia fechada.** Un documento que dice que algo está desplegado, activo o en producción lleva una fecha ISO y un puntero verificable (sha de commit, `#PR`, o una ruta en `docs/operations/`). Si todavía no hay evidencia, se escribe como propuesta, en futuro.
- La transición a `review` valida la regla anterior sobre los documentos que la rama cambió, por contenido y no por nombre de archivo. Quedan exentos por género `docs/contracts/` y `docs/design/`: describen una interfaz y una propuesta, no el estado vigente.
