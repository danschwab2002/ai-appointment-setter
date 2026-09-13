# Protocolo de cierre y aprendizaje de Johanna

- **Estado de política:** Aceptado
- **Madurez operacional:** En validación hasta aplicarse en trabajo funcional de Johanna y contrastarse con una segunda implementación.
- **Ámbito:** Toda tarea que implemente, pruebe, despliegue, diagnostique o decida comportamiento necesario para completar Johanna.
- **Índice integrador:** `docs/operations/johanna-completion-ledger.md`
- **Plantilla:** `docs/operations/templates/work-item-learning-record.md`

## 1. Propósito

Terminar Johanna artesanalmente sin perder el conocimiento necesario para implementar al siguiente aliado. Cada tarea cubierta debe producir tanto el resultado funcional como su evidencia y la disposición explícita de sus aprendizajes.

Este protocolo no define todavía un pipeline universal ni autoriza automatización, despliegues o cambios productivos.

## 2. Regla de cierre

Una tarea cubierta no está terminada sólo porque el cambio funciona. Antes de reportarla como cerrada, el agente debe:

1. crear su registro exclusivo `docs/operations/johanna-completion/records/<task_id>.md`;
2. declarar comportamiento esperado, límite verificado y estado real;
3. preservar evidencia reproducible y sanitizada;
4. separar síntoma, hipótesis y causa confirmada;
5. registrar contención y rollback cuando exista un efecto operativo;
6. clasificar cada aprendizaje;
7. actualizar el artefacto reusable correspondiente dentro del mismo alcance o dejar en su registro un pendiente explícito con owner y destino;
8. comprobar que su registro y los documentos enlazados no declaran más de lo demostrado.

Si alguno de estos puntos falta, el estado máximo es `validando` o `bloqueado`, no `cerrado`.

## 3. Inicio de una tarea diaria

Después del preflight multiagente y antes de editar:

1. declarar en el claim el recurso semántico único `johanna-completion:<task_id>`; no usar un recurso singleton compartido;
2. usar el `task_id` del claim como identificador estable;
3. reclamar y crear `docs/operations/johanna-completion/records/<task_id>.md` desde la plantilla;
4. fijar el comportamiento esperado y la evidencia de cierre;
5. identificar los documentos, contratos, pruebas y recursos externos afectados;
6. declarar incertidumbres y aprobaciones externas pendientes;
7. incluir en el claim los demás paths documentales que probablemente cambiarán.

No reclamar ni editar el ledger central desde tareas paralelas. El integrador lo actualiza serialmente a partir de registros ya integrados. No ampliar el scope de otro claim para documentar por conveniencia. Si un aprendizaje requiere tocar un artefacto reservado, registrar un pendiente de promoción con owner y destino; no editarlo concurrentemente.

## 4. Durante diagnóstico e implementación

Registrar hechos cuando cambien la decisión o el diagnóstico; no reconstruirlos al final.

Mantener separados:

- **Síntoma:** observación exacta y reproducible.
- **Hipótesis:** explicación aún no confirmada.
- **Causa confirmada:** explicación respaldada por evidencia.
- **Cambio:** código, configuración o procedimiento aplicado.
- **Verificación:** prueba que demuestra el resultado y su límite.

No guardar secretos, PII, payloads completos ni evidencias sensibles en Git. Usar referencias sanitizadas, IDs sintéticos, hashes o conteos cuando correspondan.

## 5. Destino obligatorio de cada aprendizaje

Cada hallazgo relevante debe clasificarse en exactamente uno de estos destinos primarios:

| Clase | Destino |
|---|---|
| Exclusivo de Johanna | Registro enlazado desde el ledger |
| Procedimiento reutilizable | Runbook o checklist canónico |
| Invariante técnica | Contrato y/o prueba de regresión |
| Decisión arquitectónica durable | ADR aceptado |
| Decisión humana o comercial | Gate manual con autoridad y evidencia requerida |
| Incidente operacional | Evidencia en `docs/operations/` y control preventivo enlazado |
| Incertidumbre abierta | Pendiente explícito; no promover como regla |

Un aprendizaje es relevante cuando cambia el diagnóstico, la secuencia operativa, una precondición, un gate, un mecanismo de prevención/recuperación o el diseño reusable. “Se comentó en una sesión” o “está en el código” no son destinos documentales suficientes. Para campos obligatorios que realmente no aplican, escribir `N/A` y una razón breve.

## 6. Evidencia mínima

La evidencia debe permitir a otra sesión determinar, sin depender de memoria conversacional:

- commit o snapshot probado;
- entorno y límites de la prueba;
- acción o comando reproducible, sin secretos;
- resultado esperado y observado;
- efectos externos permitidos y delta final;
- estado de flags, gates y rollback;
- qué no fue probado;
- enlace al contrato, prueba, runbook o decisión actualizado.

Distinguir siempre entre local, production-like, desplegado, activado y E2E físico.

## 7. Incidentes y frenos

Crear o actualizar un registro operacional cuando haya:

- comportamiento productivo inesperado;
- fallo que requiera diagnóstico no trivial;
- dependencia externa que bloquee el avance;
- rollback, contención o activación fallida;
- contradicción entre documentación y realidad;
- conocimiento tácito necesario para completar un paso.

El registro debe incluir síntoma, impacto, cronología mínima, evidencia, causa confirmada o estado de investigación, contención, corrección, verificación, prevención y pendientes. No fabricar causa raíz para cerrar el documento.

## 8. Revisión de cierre

Antes de mover una tarea a `review` o informar que está terminada:

- [ ] `uv run python scripts/agent_workspace.py preflight` pasa.
- [ ] El registro exclusivo usa el `task_id`, está dentro del claim y completa todas las secciones obligatorias.
- [ ] La evidencia de cierre está enlazada y sanitizada.
- [ ] Síntoma, hipótesis y causa no están mezclados.
- [ ] Los límites no probados están declarados.
- [ ] Cada aprendizaje tiene clasificación y destino.
- [ ] Tests, contratos, arquitectura, runbooks o ADR fueron actualizados cuando corresponde.
- [ ] Los pendientes de promoción tienen owner y artefacto destino en el registro.
- [ ] No se declara producción, activación o E2E sin evidencia real.
- [ ] No se tocó trabajo concurrente fuera del claim.

La transición del claim a `review` ejecuta una validación mecánica: para trabajo detectado como Johanna exige el recurso único `johanna-completion:<task_id>`, el path exacto del registro y contenido no vacío en todas las secciones de la plantilla. Esta validación comprueba presencia y estructura; el revisor/integrador comprueba la calidad y veracidad.

## 9. Revisión por hito

Al cerrar cada hito funcional de Johanna, el integrador actualiza y revisa el ledger completo usando únicamente registros integrados, y responde:

1. ¿Qué problema volvería a aparecer con otro aliado si no cambiamos nada?
2. ¿Qué control lo previene ahora y dónde está verificado?
3. ¿Qué sigue dependiendo de conocimiento tácito?
4. ¿Qué conclusión sigue siendo sólo una hipótesis?
5. ¿Qué parte debe probarse manualmente con ATT1 antes de considerarla canónica?

El procedimiento general sigue siendo provisional hasta que una segunda implementación lo valide.