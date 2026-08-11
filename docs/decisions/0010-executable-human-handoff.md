# ADR-0010: Handoff humano ejecutable con stop durable y proyección reconciliable

- **Estado:** Aceptada
- **Fecha:** 2026-08-10
- **Complementa:** ADR-0002, ADR-0003, ADR-0007 y ADR-0008
- **Estado de implementación:** Implementado en el árbol; no desplegado

## Contexto

El motor durable podía detectar takeover humano y producir motivos de escalación, pero no convertía una sugerencia autorizada en una pausa durable más trabajo visible para un operador. Ejecutar primero en Chatwoot dejaba una carrera en la que el outbound podía continuar si la proyección fallaba.

No existe una transacción distribuida entre PostgreSQL y Chatwoot. La solución debe asumir reintentos, caídas después de un efecto remoto y resultados inciertos, sin duplicar notas ni sobrescribir trabajo humano.

## Decisión

Para casos del piloto Lancemos que ya tienen `recovery_cases.conversation_id` canónico:

1. PostgreSQL es la autoridad del handoff y confirma primero.
2. La RPC `request_human_handoff` bloquea el agregado en orden contacto → caso → secuencia → acción → intento; valida el binding inmutable del scope piloto y la identidad Chatwoot; pausa caso, secuencia y conversación; cierra reservas previas a request; y conserva requests ya iniciados como `delivery_unknown`.
3. Hermes sólo puede devolver `suggest_handoff` con uno de tres motivos: `explicit_human_request`, `commercial_exception` o `policy_requires_human`. El bridge valida el contrato y la política; el modelo no llama herramientas ni decide el efecto.
4. Cada request fija una copia inmutable de scope, account, inbox, conversación externa, equipo y nota versionada. La desactivación de una política impide admisiones nuevas, pero no detiene el drain de efectos persistidos.
5. Chatwoot es una proyección reconciliable con exactamente dos efectos independientes: `assignment` y `private_note`.
6. La asignación respeta un assignee humano, acepta el equipo esperado, registra conflicto ante otro equipo y sólo asigna cuando no existe owner.
7. La nota es privada, usa marcador estable, escanea el historial antes de crear y nunca repite un POST cuyo resultado fue incierto. Múltiples marcadores son conflicto.
8. No se crean conversaciones, labels, macros ni mensajes externos al contacto.
9. Admisión y proyección tienen flags separados y default-off. La admisión requiere proyección, outbound dispatcher y perímetro piloto configurados. La proyección puede continuar en drain con admisión apagada.

## Consecuencias

- Un fallo de Chatwoot no reactiva automatización.
- Una entrega outbound que ganó `request_started` antes del handoff se conserva y reconcilia; la pausa del caso impide sucesores.
- Los operadores reciben una tarea visible sin perder asignaciones existentes.
- Se agregan tablas, RPCs y un worker de proyección con leases, fencing, reintentos, conflictos, `delivery_unknown` y dead letter.
- `/ready` expone sólo conteos sanitizados de backlog cuando la proyección está habilitada.
- La activación real requiere migración aplicada, policy publicada con scope/equipo/nota, IDs reales y E2E controlado. Este ADR no acredita esas acciones.

## Alternativas descartadas

- **Chatwoot primero:** descartada porque una falla posterior podía dejar outbound activo.
- **Macro o labels:** descartados por efectos compuestos y riesgo de sobrescribir estado operativo.
- **Asignar una persona fija:** descartado; el piloto asigna un equipo y respeta personas ya presentes.
- **Mensaje automático al contacto:** descartado; el handoff V1 mantiene silencio externo.
- **Hermes con autoridad directa:** descartado por la frontera determinística de ADR-0003/0007.
