# Transcripcion de audios entrantes de Chatwoot v1

## Problema

Un lead que contesta con una nota de voz de WhatsApp manda un `message_created`
con `content: null` y un adjunto de audio. El worker de Chatwoot armaba el
contexto del agente solo con texto (`_shadow_context`), y ante un `content`
vacio terminaba el trabajo sin responder, sin derivar y sin loguear. La
normalizacion del historial (`_normalize_chatwoot_history`) tambien saltea los
mensajes sin texto, asi que el agente nunca sabia que el audio existia.

Capturado en produccion (webhooks guardados por el bridge en `CAPTURE_DIR`):
audios sin respuesta en las conversaciones 124, 138, 174, 177 y 200 del inbox 9
entre el 22 y el 28/09/2026. Fixture:
`tests/fixtures/chatwoot_message_created_audio_inbox_9_conv_200_20260928.json`.

## Entrada: el adjunto de Chatwoot

Chatwoot v4.13.0, canal WhatsApp Cloud. Por cada audio entrante:

| Campo | Valor observado |
|---|---|
| `content` | `null` |
| `attachments[].file_type` | `"audio"` |
| `attachments[].extension` | `"ogg"` |
| `attachments[].content_type` | `"audio/opus"` |
| `attachments[].transcribed_text` | `""` (Chatwoot no transcribe) |
| `attachments[].file_size` | bytes (79.794 para 36 s) |
| `attachments[].data_url` | `https://<host de CHATWOOT_BASE_URL>/rails/active_storage/blobs/redirect/<id firmado>/<archivo>` |

`data_url` responde `302` a `/rails/active_storage/disk/...` en el mismo host y
despues `200 audio/opus`, sin autenticacion. El contenido es un contenedor Ogg
(`OggS`) con Opus (`OpusHead`).

El mismo mensaje llega por la API de mensajes
(`GET /api/v1/accounts/{account}/conversations/{id}/messages`) con
`message_type: 0` y los mismos `attachments`.

## Salida: OpenRouter

`POST https://openrouter.ai/api/v1/audio/transcriptions`, con
`Authorization: Bearer $OPENROUTER_API_KEY` y cuerpo JSON:

```json
{
  "model": "openai/whisper-large-v3-turbo",
  "input_audio": {"data": "<audio en base64>", "format": "ogg"},
  "language": "es"
}
```

Respuesta esperada: `{"text": "...", "usage": {"seconds": ..., "cost": ...}}`.
La respuesta real capturada esta en
`tests/fixtures/openrouter_audio_transcription_response_20260929.json`.

Limites del proveedor: 25 MB por pedido y 60 s de procesamiento. El bridge corta
en 16 MB de audio (el base64 agrega un tercio).

## Comportamiento

1. **Descarga.** Solo desde el host de `CHATWOOT_BASE_URL`, por `https`, y solo
   siguiendo redirecciones que se quedan en ese host. Si `file_size` o lo
   descargado supera el limite, no se transcribe.
2. **Modelos en orden.** `AUDIO_TRANSCRIPTION_MODELS`, por defecto
   `openai/whisper-large-v3-turbo` y, de respaldo,
   `microsoft/mai-transcribe-2` (proveedores distintos a proposito). Un
   modelo que responde con error HTTP, JSON invalido, falla de transporte o
   texto vacio da paso al siguiente. El audio se baja una sola vez.
3. **Cache.** Cada transcripcion se guarda en
   `AUDIO_TRANSCRIPTION_CACHE_DIR/<attachment_id>.json`. El historial se rearma
   desde Chatwoot en cada turno y un audio ya transcripto no se vuelve a pagar.
4. **Lo que ve el agente.** El mensaje entra con el texto
   `[Audio transcrito] <transcripcion>`, tanto el que dispara el turno como los
   audios anteriores del historial (hasta 5 transcripciones nuevas por turno).
5. **Si fallan todos los modelos** con el audio que dispara el turno, el agente
   no corre y la conversacion se deriva a una persona con
   `detail_reason_code = audio_transcription_failed` (requiere
   `HUMAN_HANDOFF_ADMISSION_ENABLED` y la admision de Corte B). Un audio viejo
   del historial que no se pudo transcribir entra como
   `[Audio que no se pudo transcribir]`, para que el mensaje no desaparezca del
   historial canonico.
6. **Transcripcion apagada.** Un entrante sin texto sigue sin llegar al agente,
   pero deja `chatwoot_inbound_without_text_ignored message=<id>` en el log.

## Configuracion

| Variable | Default | Uso |
|---|---|---|
| `CHATWOOT_AUDIO_TRANSCRIPTION_ENABLED` | `false` | prende la transcripcion |
| `OPENROUTER_API_KEY` | vacio | obligatoria con la transcripcion prendida |
| `AUDIO_TRANSCRIPTION_MODELS` | `openai/whisper-large-v3-turbo,microsoft/mai-transcribe-2` | orden de prueba |
| `AUDIO_TRANSCRIPTION_CACHE_DIR` | `./data/audio-transcriptions` | cache por adjunto |

## Log

| Linea | Cuando |
|---|---|
| `audio_transcription_ok attachment= model= bytes= seconds= cost=` | un modelo transcribio |
| `audio_transcription_model_failed attachment= model= reason=` | un modelo fallo y se prueba el siguiente |
| `chatwoot_audio_transcription_failed message= reason=` | fallaron todos con el audio del turno |
| `audio_transcription_history_failed message= reason=` | fallo un audio viejo del historial |
| `chatwoot_inbound_without_text_ignored message=` | entrante sin texto que no llega al agente |

## Fuera de alcance

- Imagenes y stickers: siguen sin llegar al agente (ahora con la linea de log).
- La frase en castellano del motivo `audio_transcription_failed` en la nota de
  derivacion: sin migracion, la nota muestra el codigo crudo, que es el
  comportamiento documentado de `inbound_handoff_reason_sentence` para un
  codigo desconocido.
