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
visibilidad, dirección, actor y JID autorizado. Cuando el runtime declara alcance
Chatwoot, exige además que `account.id`, `inbox.id` y
`conversation.inbox_id` coincidan exactamente con `CHATWOOT_ACCOUNT_ID` y
`CHATWOOT_INBOX_ID`. Los dos identificadores de inbox deben estar presentes y ser
enteros positivos no booleanos y coherentes. JSON `true` se rechaza aunque Python
lo compare igual a `1`; no existe fallback por nombre de inbox, proveedor ni
teléfono. Un evento del mismo JID en otro inbox queda rechazado antes de captura,
pausa, Hermes o cualquier efecto.

Si sólo uno de los dos IDs esperados está configurado, el ingreso falla cerrado
con `reason=scope_configuration_incomplete`. Los reason codes de rechazo de
alcance son `account_not_allowed` e `inbox_not_allowed`; no contienen IDs ni PII.
La omisión total de ambos IDs se conserva únicamente para el modo local legacy
inyectado directamente mediante `Settings`; `Settings.from_env` siempre requiere
account y una configuración productiva WABA debe declarar también inbox.

Un mensaje entrante sólo puede admitirse si contiene un `id` canónico entero no
negativo; si falta o es inválido, responde `200 ignored` con
`reason=invalid_message_id` y no crea trabajo durable.

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
6. Un lock por delivery protege trabajo sin agrupación; un lock hasheado por
   conversación serializa los turnos agrupados sin exponer el ID en el nombre.
7. El archivo pasa a `completed` únicamente después de un resultado terminal.
8. Al completar, el payload se elimina del archivo de trabajo y queda sólo el
   tombstone mínimo necesario para deduplicar el delivery.

Los mensajes públicos entrantes aceptados se agrupan por conversación antes de
invocar Hermes. Cada nueva admisión reinicia una ventana durable configurada por
`CHATWOOT_INBOUND_DEBOUNCE_SECONDS`, cuyo valor productivo inicial es `30`. El
endpoint no espera esa ventana: conserva el ACK inmediato después de persistir.

La admisión más reciente determina cuándo vence el silencio. El delivery con el
mayor ID canónico de Chatwoot lidera el turno aunque los webhooks hayan llegado
fuera de orden. El historial leído hasta ese ID debe contener todos los IDs del
batch; la ausencia de cualquiera es reintentable y nunca completa silenciosamente
un turno parcial. El cliente recorre las páginas de 20 mensajes mediante el cursor
`before`. Lee como mínimo 200 mensajes recientes y continúa más atrás hasta
encontrar todos los IDs obligatorios, alcanzar el inicio comprobado del historial
o agotar un límite operacional de 100 páginas. Devuelve la ventana reciente más
los miembros requeridos encontrados. Los deliveries
anteriores se completan sin invocar Hermes por separado sólo después del éxito del
líder. El valor `0` desactiva la agrupación y
restaura el procesamiento inmediato.

La formación del grupo también considera deliveries temporalmente diferidos por
backoff. Si llega un mensaje más reciente, esos deliveries anteriores se cierran
como miembros del mismo turno y no reaparecen luego como evaluaciones obsoletas.
Después de adquirir el lock conversacional, el worker vuelve a escanear todos los
miembros y revalida leader, backoff y deadline; esa relectura es el punto de corte
del turno. Una admisión posterior forma el turno siguiente y la autorización final
impide enviar una respuesta basada en el trigger anterior.
Mientras el líder se reintenta, los miembros anteriores permanecen `admitted`.
Si un error no reintentable agota los intentos del líder, todos terminan `failed`
en lugar de tombstones `completed`. Esa transición multiarchivo usa un journal de
intención privado y sincronizado a disco; un reinicio reconcilia cualquier journal
pendiente antes de volver a seleccionar trabajo.

La ejecución interna es **at-least-once**. Los resultados persistidos de Hermes y
los marcadores idempotentes del sender impiden repetir el razonamiento o el efecto
externo cuando un replay ocurre después de un éxito parcial.

## División de respuesta saliente

Cuando `CHATWOOT_REPLY_SPLITTER_ENABLED=true`, una propuesta pública validada se
envía a un divisor de formato mediante el mismo API server Hermes, con `provider`
y `model` explícitos. Su única salida admitida es `{"parts":[...]}` con 1–4
strings no vacíos. El bridge recorre el reply original por cursor: cada parte
debe coincidir carácter por carácter y sólo puede omitir whitespace que ya
existía entre dos partes. Whitespace interno modificado, salida inválida o error
HTTP del modelo persisten un manifiesto fallback con la respuesta original en una
parte. Si ese manifiesto no puede persistirse, no se autoriza ningún POST.
La frontera de aplicación vuelve a validar y materializa atómicamente la salida de
cualquier implementación inyectada del divisor —incluido su fallback por
excepción— antes de autorizar el envío.

Una división válida se persiste privadamente antes del primer envío como un
manifiesto inmutable versión 1. Su identidad es el hash de conversación + trigger
canónico, no el delivery del webhook. El manifiesto contiene hash del reply,
cantidad total y, para cada parte ordenada, contenido, hash de contenido e
identidad de parte. Las identidades son las mismas que usa el sender para markers
y journals. En replay se reutiliza el mismo manifiesto; un reply diferente para
la misma identidad falla cerrado con `reply_split_manifest_conflict` y no autoriza
un nuevo lote. Un cache existente inválido, inseguro o inaccesible falla cerrado:
nunca autoriza enviar sin manifiesto ni recalcular el lote después de un envío parcial. El directorio,
locks y resultados se validan por tipo y owner sin seguir symlinks. Para
multipart, cada POST incluye:

Antes de escribir el manifiesto, el bridge sincroniza una claim hash-only
independiente en `REPLY_DIR`. Una claim sin su manifiesto correspondiente produce
`reply_split_manifest_missing_after_claim`; no consulta al divisor ni autoriza
ningún POST.

`CHATWOOT_REPLY_SPLITTER_ENABLED` controla sólo la creación de manifiestos nuevos.
El bridge consulta y respeta un manifiesto existente aun cuando el flag esté
apagado, para que un restart o rollback no cambie un lote parcial a una sola parte.
Si ya existe el journal legacy de una respuesta única para el mismo lote, una
geometría multipart nueva queda bloqueada como entrega desconocida hasta que el
marker previo pueda reconciliarse.

- hash estable del lote lógico;
- hash idempotente de la parte;
- índice 1-based;
- cantidad total.

La primera parte no espera. Cada parte posterior espera
`CHATWOOT_REPLY_PART_DELAY_SECONDS` — valor inicial `2` — y luego repite la
autorización completa. Sólo se toleran entre el trigger y la parte actual las
partes anteriores, válidas y contiguas, del mismo lote. Un inbound nuevo,
intervención pública humana, pausa o mensaje ajeno bloquea las partes restantes.
Cada POST se precede con un journal durable hash-only `posting`. Una respuesta
HTTP perdida nunca habilita retry ciego: el trabajo permanece admitido y los
replays sólo reconcilian el marker exacto en el historial canónico de Chatwoot.
El journal no se elimina, por lo que un marker temporalmente invisible o borrado
tampoco habilita otro POST. La lectura falla cerrada si agota 100 páginas sin
alcanzar 2000 mensajes únicos o una frontera real, incluso si páginas solapadas
ya incluyeron el trigger.

## Fallos y reintentos

Una excepción del procesamiento conserva el trabajo y registra únicamente el tipo
de error y el estado, sin delivery ID, mensaje, teléfono ni payload.
Los fallos HTTP o de protocolo al obtener el historial canónico de Chatwoot, y
la ausencia temporal del mensaje trigger en ese historial, son reintentables y
no se agotan ni producen un resultado terminal de Hermes. Agotar las 100 páginas
sin encontrar IDs obligatorios es un error acotado sujeto al máximo general de
intentos, no un retry infinito. Para otros errores:

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
bloquea la respuesta del webhook. Esta intervención no queda demorada por la
ventana de mensajes entrantes.

## Privacidad e idempotencia

- directorio de trabajo: `0700`;
- archivos: `0600`;
- directorio, archivos y locks se abren sin seguir symlinks y validando tipo y
  propietario;
- timestamps durables no finitos se rechazan y nunca llegan al handler;
- nombre: SHA-256 del delivery ID;
- no se registran payloads, contenido, JIDs ni identificadores personales;
- un delivery repetido no crea un segundo archivo;
- una respuesta externa se correlaciona y valida antes de considerarse aceptada.

El lifecycle detiene todos los workers dentro de `finally`, incluso si la
aplicación termina por excepción. Cada poll hace un scan inicial; para trabajo
agrupado, la revalidación bajo lock vuelve a leer todos los miembros admitidos de
la conversación antes de decidir el turno.
