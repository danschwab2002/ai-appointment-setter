# Registro de trabajo y aprendizaje — johanna-documentation-operating-protocol-v1

- **Estado:** `validando`
- **Owner:** `hermes-session-20260913`
- **Claim:** `johanna-documentation-operating-protocol-v1`
- **Rama:** `docs/johanna-documentation-operating-protocol-v1`
- **Base:** `origin/main@5b303954f62f0f0c9a950d3e5e44ad77715c94b7`
- **Recurso:** `johanna-completion:johanna-documentation-operating-protocol-v1`

## Resultado esperado

Toda futura tarea que implemente, pruebe, despliegue, diagnostique o decida comportamiento necesario para completar Johanna debe conservar un registro exclusivo, verificable y reusable antes de pasar a `review`, sin obligar a tareas paralelas a editar un ledger compartido.

## Prerrequisitos y gates

- El proyecto mantiene claims y worktrees exclusivos mediante `scripts/agent_workspace.py`.
- La política no autoriza commit, push, merge, deploy, migraciones ni efectos productivos.
- El protocolo queda operacionalmente `validando` hasta utilizarse en una tarea funcional real de Johanna.

## Ejecución y evidencia

- Se añadió el protocolo, una plantilla, un ledger mantenido serialmente por el integrador y reglas breves en `AGENTS.md` y la política documental.
- Se añadió al coordinador un gate al transicionar a `review`.
- El gate detecta trabajo de Johanna, exige el recurso único `johanna-completion:<task_id>`, exige el path determinístico `docs/operations/johanna-completion/records/<task_id>.md` y valida sus secciones obligatorias.
- Evidencia local: `uv run pytest tests/test_agent_workspace.py -q` pasó con 44 pruebas después de observar los fallos RED por ausencia de validación, falta de integración con `review`, symlinks directos o ancestrales y contenido placeholder.
- No probado por esta evidencia local: CI remoto ni primera aplicación en una tarea funcional de Johanna. La publicación y el review pin se verifican en el claim y GitHub, sin intentar auto-referenciar el hash desde el mismo commit.
- Efectos externos: ninguno.

## Diagnóstico

- **Síntoma:** el primer diseño exigía que todas las tareas paralelas modificaran un ledger singleton y no existía gate mecánico de cierre.
- **Hipótesis inicial:** `AGENTS.md` más una skill bastarían para sostener el comportamiento.
- **Causa confirmada:** la regla documental chocaba con claims de path exclusivo y la transición a `review` no validaba presencia ni estructura de evidencia.
- **Evidencia causal:** revisión independiente de los cinco artefactos iniciales y lectura de los guards actuales de `AgentWorkspace.transition`.

## Cambio, contención y rollback

- Se reemplazó la escritura concurrente del ledger por registros y recursos determinísticos por `task_id`; el integrador actualiza el índice serialmente.
- Se añadió una validación estructural al paso a `review`, con tests para recurso ausente, path ausente, secciones incompletas, registro completo y tarea no relacionada.
- Contención: la validación sólo se activa para claims detectados como Johanna o que declaren `johanna-completion`; no afecta el runtime comercial.
- Rollback: revertir el commit futuro de esta rama eliminaría política, artefactos y gate sin tocar datos ni servicios. No existe todavía commit que revertir.

## Disposición de aprendizajes

| Hallazgo | Clase | Destino | Estado |
|---|---|---|---|
| Un ledger singleton bloquea trabajo paralelo | Procedimiento reutilizable | Protocolo y ledger integrador | Aplicado localmente |
| Una instrucción sin gate puede omitirse | Invariante de coordinación | `scripts/agent_workspace.py` y tests | Aplicado localmente |
| Presencia estructural no prueba calidad | Gate manual | Revisión del integrador | Documentado |
| El protocolo aún no fue usado en trabajo funcional | Incertidumbre abierta | Primera tarea Johanna posterior a integración | Pendiente |
| `SOUL.md` no corresponde a gobernanza de repositorio | Decisión de alcance | `AGENTS.md` + skill del perfil de ingeniería | Aplicado; sin cambio comercial |

## Documentos afectados

- `AGENTS.md`: trigger obligatorio y gate resumido.
- `docs/documentation-governance.md`: política diaria.
- `docs/operations/johanna-completion-learning-protocol.md`: procedimiento autoritativo.
- `docs/operations/johanna-completion-ledger.md`: índice serial del integrador.
- `docs/operations/templates/work-item-learning-record.md`: estructura mínima reusable.
- `scripts/agent_workspace.py` y `tests/test_agent_workspace.py`: enforcement y regresión.
- Arquitectura, contratos y ADR: `N/A`; no cambia comportamiento de producto ni una decisión arquitectónica del runtime.

## Criterio de cierre

- [x] Resultado funcional local verificado.
- [x] Evidencia y límites declarados.
- [x] Síntoma, hipótesis y causa separados.
- [x] Concurrencia resuelta mediante registros exclusivos.
- [x] Gate mecánico cubierto por pruebas RED/GREEN.
- [x] Aprendizajes clasificados y destinados.
- [x] Candidato local listo para commit; publicación, PR y review pin se verifican como estados externos del claim.
- [ ] Validación en una tarea funcional real de Johanna: pendiente posterior a integración.
