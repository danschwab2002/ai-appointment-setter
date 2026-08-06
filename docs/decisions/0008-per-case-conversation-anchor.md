# ADR-0008: Autoridad de conversación por caso, no por identidad

- **Estado:** Aceptada
- **Fecha:** 2026-08-05
- **Refina:** ADR-0007 (secciones 6, 11 y 12)

## Contexto

El motor durable de recuperación usa la conversación canónica para preguntarle a
Chatwoot «¿el comprador respondió?» antes de enviar un `no_reply_review`.
Originalmente esa autoridad se cargaba desde
`channel_identities.external_conversation_id`, un campo compartido por todos los
casos del mismo número de WhatsApp.

Ese diseño asumía de hecho «una identidad ↔ una conversación para siempre»:
la primera aceptación estampaba la conversación en la identidad y
`record_and_finalize_followup_acceptance` rechazaba con
`channel_identity_conversation_mismatch` cualquier conversación posterior.

### Evidencia operativa

El mismo comprador abandonó el carrito dos veces. Chatwoot abrió una conversación
nueva por sesión:

```text
caso #1 → conversación 26
caso #2 → conversación 27 (mismo contacto e identidad WhatsApp)
```

El primer contacto del caso #2 se envió a la conversación 27, pero la identidad
seguía estampada con 26. La finalización abortó con HTTP 400 y el intento quedó en
`request_started`, sin mensaje canónico ni sucesor durable.

### Hallazgo de revisión independiente

Una solución intermedia proponía avanzar el campo de la identidad a la
conversación del caso nuevo. La revisión detectó que era insegura:
`plan_cart_recovery` reutiliza casos por
`(contacto, fuente, producto, oferta)`, mientras que la identidad se comparte por
número. Un comprador puede tener dos casos vivos simultáneos para productos u
ofertas distintos.

Si el caso A usa conversación 26 y el caso B avanza la identidad a 27, el
`no_reply_review` de A consulta por error la conversación 27. Esto es un
**secuestro de autoridad entre casos** y puede producir decisiones silenciosamente
incorrectas, además de romper la finalización posterior de A.

Por lo tanto, serializar o avanzar el campo compartido no resuelve el problema. La
autoridad debe vivir en la fila propietaria: el caso.

## Decisión

La fuente de autoridad de conversación es
`recovery_cases.conversation_id`. Cada caso posee su conversación canónica y los
casos concurrentes de una identidad no comparten autoridad.

Reglas concretas:

1. **Contexto autoritativo por caso.**
   `get_followup_chatwoot_context` obtiene el ID externo mediante la conversación
   vinculada en `recovery_cases.conversation_id`, nunca desde
   `channel_identities.external_conversation_id`.

2. **Primer contacto sin conversación previa.**
   Un `first_contact_review` cuyo caso todavía tiene
   `conversation_id IS NULL` toma el camino limpio de primera conversación. No
   hereda ni consulta la conversación de otro caso de la misma identidad.

3. **Evidencia validada contra el caso.**
   `reevaluate_followup_action` valida que
   `p_chatwoot_conversation_id` coincida con la conversación canónica del caso.
   La coincidencia con el campo de la identidad no constituye autoridad.

4. **Finalización canónica por caso.**
   `record_and_finalize_followup_acceptance` puede crear o seleccionar una
   conversación distinta para otro caso de la misma identidad. Los guards
   `case_conversation_mismatch` y `sequence_conversation_mismatch` permanecen:
   un caso o secuencia ya vinculados nunca pueden saltar de conversación.

5. **Campo de identidad no autoritativo.**
   `channel_identities.external_conversation_id` se conserva como puntero
   desnormalizado de última escritura por compatibilidad e índice existente.
   Puede avanzar entre conversaciones, pero ningún chequeo de respuesta ni
   finalización lo usa como fuente de autoridad.

6. **Casos concurrentes soportados.**
   Dos casos vivos de la misma identidad para productos u ofertas distintos
   conservan conversaciones y chequeos de respuesta independientes.

## Invariantes

- Un caso tiene como máximo una conversación canónica, y esa conversación pertenece
  al mismo contacto e identidad seleccionada.
- Una secuencia tiene como máximo una conversación canónica y coincide con la del
  caso.
- Un `no_reply_review` sólo consulta la conversación del mismo caso.
- `human_takeover`, `paused_human` y el estado de automatización de Chatwoot sólo
  pausan el caso dueño de esa conversación. Una pausa global del contacto debe
  modelarse explícitamente en estado/autorización del contacto.
- La identidad puede agrupar múltiples conversaciones históricas o concurrentes;
  su puntero desnormalizado no decide autoridad.
- La aceptación canónica sigue exigiendo un mensaje único, outbound, aceptado y
  correlacionado con el intento y la acción durables.

## Consecuencias

### Positivas

- Un comprador recurrente puede iniciar una recuperación nueva sin HTTP 400.
- Dos carritos concurrentes para productos distintos no se pisan autoridad.
- La respuesta del comprador en un caso sólo detiene la automatización de ese
  caso.
- La cadena mensaje 1 → 2 → 3 puede persistir y avanzar sobre su conversación.
- Se conservan todos los guards canónicos dentro de cada caso y secuencia.

### Costos

- Se reemplazan tres funciones PL/pgSQL:
  `get_followup_chatwoot_context`, `reevaluate_followup_action` y
  `record_and_finalize_followup_acceptance`.
- Todo consumidor futuro debe tratar `recovery_cases.conversation_id` como la
  autoridad y `channel_identities.external_conversation_id` sólo como dato
  desnormalizado.
- El campo desnormalizado queda como deuda de compatibilidad; una migración futura
  puede renombrarlo o eliminarlo cuando no existan consumidores externos.

## Pruebas exigidas

1. Reproducción RED del comprador recurrente: conversación 26 seguida de 27.
2. Dos casos **vivos simultáneos**, misma identidad, productos distintos:
   después de finalizar B en 27, el contexto de A sigue devolviendo 26.
3. Un salto de conversación dentro del mismo caso permanece bloqueado.
4. El inbound autoritativo se valida contra la conversación del caso.
5. Harness SQL completo y suite Python sin regresiones.
6. Concurrencia real en PostgreSQL antes de afirmar verificación de locks en
   producción.

## Relación con la robustez de finalización

En paralelo se corrigió una brecha independiente: si el sender ya devolvió un
mensaje aceptado pero falla la persistencia canónica, el intento se resuelve a
`delivery_unknown` con deadline acotada, conservando el `remote_message_id`
conocido para reconciliación. Nunca debe quedar huérfano en `request_started` ni
reintentarse el envío a ciegas.

## Alternativas descartadas

### Mantener una conversación permanente por identidad

Incorrecto para Chatwoot, que abre conversaciones nuevas por sesión y para
compradores recurrentes.

### Avanzar el ancla compartida sólo cuando el caso no tiene conversación

Descartado tras revisión independiente. Falla cuando dos casos vivos de productos
u ofertas distintos comparten identidad: el último avance secuestra la autoridad
del otro caso.

### Fallar cerrado ante casos concurrentes

Era una mitigación segura pero incompleta. Se eligió resolver la propiedad de la
autoridad correctamente en vez de prohibir un caso de negocio válido.

## Documentos relacionados

- [ADR-0007: motor durable de próxima acción](0007-durable-next-action-engine.md)
- [ADR-0002: human takeover](0002-human-takeover-detection.md)
- [Contrato V1 del motor](../contracts/followup-engine-v1.md)
