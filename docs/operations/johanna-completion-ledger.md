# Ledger de finalización y aprendizaje de Johanna

- **Estado:** Activo
- **Propósito:** Índice autoritativo del trabajo necesario para completar Johanna y de los aprendizajes que deben promoverse antes de una segunda implementación.
- **Protocolo:** `docs/operations/johanna-completion-learning-protocol.md`

## Reglas

- Cada tarea cubierta crea un registro exclusivo nombrado con su `task_id` antes de pasar a `review`.
- El integrador actualiza este ledger serialmente a partir de registros ya integrados; las tareas paralelas no lo editan salvo claim exclusivo.
- El ledger enlaza evidencia; no duplica documentos operativos extensos.
- Sólo `cerrado` significa que resultado, evidencia y disposición de aprendizajes están completos.
- Los estados permitidos son `pendiente`, `implementando`, `bloqueado`, `validando` y `cerrado`.
- Una dependencia humana o externa debe nombrar autoridad/owner y condición de desbloqueo.
- Si no hay causa confirmada, conservar la incertidumbre; no convertir una hipótesis en regla.

## Definición del alcance de cierre

La definición verificable de “Johanna completa” debe acordarse y enlazarse aquí antes de usar el total de filas como porcentaje de avance.

- **Documento de alcance:** Pendiente
- **Autoridad de aceptación:** Pendiente
- **Fecha o versión aceptada:** Pendiente

Hasta entonces, este ledger registra trabajo y aprendizaje, pero no demuestra por sí mismo que Johanna esté completa.

## Trabajo de cierre

| Task ID | Área | Comportamiento esperado | Estado | Owner | Evidencia de cierre | Bloqueo o incertidumbre | Registro detallado |
|---|---|---|---|---|---|---|---|
| johanna-documentation-operating-protocol-v1 | Gobernanza | Toda nueva tarea de cierre captura evidencia y dispone sus aprendizajes antes de pasar a review | validando | Integrador del proyecto | Protocolo, regla mecánica y pruebas del coordinador | Falta validar el protocolo durante una tarea funcional de Johanna | `docs/operations/johanna-completion/records/johanna-documentation-operating-protocol-v1.md` |

## Aprendizajes pendientes de promoción

| ID | Hallazgo | Clasificación | Destino requerido | Owner | Estado | Evidencia o referencia |
|---|---|---|---|---|---|---|
| LP-001 | Los incidentes históricos de Johanna están distribuidos y deben reconciliarse sin reescribir su evidencia | Incertidumbre abierta | Inventario retrospectivo enlazado desde este ledger | Pendiente | pendiente | Auditoría documental de preparación ATT1; incorporar sólo con revisión de fuentes |

## Decisiones y gates externos pendientes

| ID | Decisión o input | Autoridad/owner | Evidencia requerida | Impacto | Estado |
|---|---|---|---|---|---|
| — | Agregar cuando una tarea encuentre una dependencia real | — | — | — | — |

## Índice de incidentes y frenos

| ID | Síntoma | Causa | Contención | Control preventivo | Etapa futura | Documento |
|---|---|---|---|---|---|---|
| — | Agregar incidentes nuevos y enlazar los históricos reconciliados | No asumir | — | — | — | — |

## Revisión por hito

Registrar una revisión cuando se cierre un hito funcional, no en cada microcambio.

| Fecha | Hito | Filas revisadas | Aprendizajes promovidos | Incertidumbres restantes | Veredicto |
|---|---|---|---|---|---|
| — | — | — | — | — | — |
