# Contrato de ingreso Chatwoot v1

- **Estado:** Implementado localmente; pendiente de despliegue y verificación HTTP productiva
- **Versión:** 1
- **Endpoint:** `POST /webhooks/chatwoot`

## Entrada

El cuerpo es el JSON crudo emitido por Chatwoot. Son obligatorios:

- `X-Chatwoot-Signature`;
- `X-Chatwoot-Timestamp`;
- `X-Chatwoot-Delivery`.

El cuerpo tiene un límite fijo de `1 MiB`, aplicado durante la lectura y antes de
autenticar la firma.

Antes de persistir, el bridge valida firma, antigüedad, JSON, tipo de evento,
visibilidad, dirección, actor y JID autorizado.

## Respuestas

| HTTP | `status` o `detail` | Significado |
|---|---|---|
| `202` | `accepted` | El trabajo fue admitido durablemente; Hermes y los efectos externos todavía pueden estar pendientes. |
| `202` | `captured` | Hermes está deshabilitado y el payload quedó capturado; no existe trabajo conversacional posterior. |
| `200` | `duplicate` | El mismo delivery ya había sido capturado o admitido. |
| `200` | `ignored` | El evento no es procesable; `reason` contiene un código estable sin PII. |
| `400` | `invalid_json` | El cuerpo no es JSON válido. |
| `401` | `invalid_signature` | La firma no coincide. |
| `401` | `invalid_timestamp` | El timestamp no es entero. |
| `401` | `stale_webhook` | El delivery excede la ventana anti-replay. |
| `413` | `chatwoot_webhook_body_too_large` | El cuerpo supera el límite de 1 MiB. |
| `422` | `invalid_conversation_id` | Una intervención humana no identifica una conversación válida. |
| `503` | `chatwoot_control_unavailable` | La intervención humana requiere control de Chatwoot pero el cliente no está configurado. |

La respuesta `accepted` tiene esta forma:

```json
{"status":"accepted","delivery_id":"..."}
```

No confirma que Hermes terminó ni que Chatwoot aceptó una respuesta.

## Admisión y replay

1. El payload aceptado se captura bajo `CAPTURE_DIR`.
2. El trabajo se escribe atómicamente bajo `CAPTURE_DIR/.work`, con permisos
   privados y `fsync` de archivo y directorio.
3. Sólo después de esa admisión el endpoint devuelve HTTP 202.
4. El worker procesa archivos `admitted` fuera de la solicitud HTTP.
5. Al reiniciar, el worker retoma admisiones pendientes.
6. Un lock por archivo evita procesamiento concurrente del mismo delivery.
7. El archivo pasa a `completed` únicamente después de un resultado terminal.
8. Al completar, el payload se elimina del archivo de trabajo y queda sólo el
   tombstone mínimo necesario para deduplicar el delivery.

La ejecución interna es **at-least-once**. Los resultados persistidos de Hermes y
los marcadores idempotentes del sender impiden repetir el razonamiento o el efecto
externo cuando un replay ocurre después de un éxito parcial.

## Fallos y reintentos

Una excepción del procesamiento conserva el trabajo y registra únicamente el tipo
de error y el estado, sin delivery ID, mensaje, teléfono ni payload.
Los fallos HTTP o de protocolo al obtener el historial canónico de Chatwoot, y
la ausencia temporal del mensaje trigger en ese historial, son reintentables y
no se agotan ni producen un resultado terminal de Hermes. Para otros errores:

- máximo: `8` intentos;
- backoff exponencial con jitter;
- espera base: aproximadamente `2` segundos;
- espera máxima: aproximadamente `60` segundos;
- timeout por ejecución: `120` segundos;
- agotamiento: estado terminal `failed`, disponible para reconciliación.

Cuando Hermes está deshabilitado, el modo de captura también escribe mediante
archivo temporal, `fsync`, publicación atómica exclusiva y `fsync` del
directorio antes de responder `202 captured`.

Una intervención humana pública usa la misma admisión durable. La etiqueta
`automation_paused` se aplica en el worker y un fallo transitorio de Chatwoot no
bloquea la respuesta del webhook.

## Privacidad e idempotencia

- directorio de trabajo: `0700`;
- archivos: `0600`;
- directorio, archivos y locks se abren sin seguir symlinks y validando tipo y
  propietario;
- nombre: SHA-256 del delivery ID;
- no se registran payloads, contenido, JIDs ni identificadores personales;
- un delivery repetido no crea un segundo archivo;
- una respuesta externa se correlaciona y valida antes de considerarse aceptada.

El lifecycle detiene todos los workers dentro de `finally`, incluso si la
aplicación termina por excepción. Cada poll hace un único escaneo del inbox; la
revalidación bajo lock lee directamente sólo el ítem seleccionado.
