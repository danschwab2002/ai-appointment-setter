# Adjunto sin texto de la persona (v1)

Estado: contrato. Lo implementa el bridge detras de
`INBOUND_ATTACHMENT_WITHOUT_TEXT_ENABLED` (apagado por defecto, portable). El
efecto en produccion se documenta en `docs/operations/` despues del E2E.

## Por que existe

El diagnostico E-314 (2026-11-03) encontro dos conversaciones (ATT1 #604 y
Maribel #6) que terminaron en una imagen sin texto y nadie contesto. Un entrante
sin texto que no es un audio salia de `process_chatwoot_work` con
`chatwoot_inbound_without_text_ignored`, sin respuesta, sin derivacion y sin
aviso; el monitor de trabadas no lo tomaba (pide texto en el ultimo mensaje) y la
guarda de envio habria rechazado la respuesta (`invalid_trigger_message`). El
audio ya tenia su camino (`chatwoot-audio-transcription-v1.md`).

Decision de Dan (2026-11-03): «Que primero pregunte que es y luego pase si llega
una segunda».

## Que es un adjunto sin texto

Un mensaje de la persona (publico, `message_type` 0 o `incoming`, remitente
`contact` en el historial) con `content` vacio o nulo y al menos un adjunto en
`attachments`, ninguno de `file_type: audio`. Un mensaje con un audio sigue el
camino del audio aunque traiga otro adjunto. El tipo sale de `file_type`:
`image` (y `image/webp`, la forma de un sticker de WhatsApp, como sticker),
`video`, `file` (documento), `location`, `contact`; cualquier otro, archivo.

## Comportamiento con el flag

1. **El primero:** el mensaje entra al agente con el texto de sistema
   `[La persona mandó <una imagen | un sticker | un video | un documento | N imágenes | N archivos> sin texto. No lo podés ver: preguntale qué es o en qué la podés ayudar.]`,
   como mensaje de la persona. El agente corre con el mismo contexto que con un
   texto (producto, estado de clienta, pago rechazado reciente) y su respuesta
   pasa por las mismas guardas.
2. **El segundo seguido:** si ningun mensaje del lote trae texto y el ultimo
   mensaje de la persona anterior al lote tambien fue un adjunto sin texto, el
   agente no corre y la conversacion se deriva a una persona por el camino
   existente (`request_current_inbound_handoff`), con
   `detail_reason_code = attachment_without_text_repeated`. Lo que el agente o
   el equipo contesto en el medio no corta la racha; un texto o un audio de la
   persona si. El lote del debounce cuenta como un solo envio: dos fotos
   mandadas juntas son una sola pregunta. Requiere
   `HUMAN_HANDOFF_ADMISSION_ENABLED` y la admision de Corte B; sin eso deja
   `chatwoot_attachment_handoff_unavailable` y no contesta.
3. **El historial:** en el turno del agente, todo adjunto sin texto de la
   persona del historial entra con su texto de sistema, para que el mensaje que
   dispara el turno (o uno del lote) no desaparezca de la normalizacion.
4. **El monitor de trabadas** toma tambien una conversacion cuyo ultimo mensaje
   publico es un adjunto sin texto de la persona; el payload que encola lleva
   `attachments`.
5. **La guarda de envio** (`_current_authorization_result`) acepta como
   disparador un adjunto sin texto de la persona, como ya acepta un audio.

Lo de antes sigue igual: la admision `blocked` (pausa, `human_takeover`), la
baja (un primer adjunto pasa por el escaneo canonico como un texto), la etiqueta
`automation_paused` y la persona asignada frenan igual que con un texto.

## Sin el flag

Nada cambia: el adjunto deja `chatwoot_inbound_without_text_ignored message=<id>`,
el monitor no lo toma y la guarda lo rechaza.

## Log

| Linea | Cuando |
|---|---|
| `chatwoot_inbound_attachment_without_text message= kinds= repeated=` | info revisado (`LOGGED_INFO_EVENTS`): cada adjunto sin texto que se procesa con el flag |
| `chatwoot_attachment_handoff_unavailable message=` | el segundo seguido sin la derivacion configurada |

## Fuera de alcance

- Ver el contenido del adjunto (OCR, vision). El agente sabe el tipo, no lo que
  muestra.
- La frase en castellano del motivo `attachment_without_text_repeated` en la
  nota de derivacion: sin migracion, la nota muestra el codigo crudo, igual que
  `audio_transcription_failed`.
