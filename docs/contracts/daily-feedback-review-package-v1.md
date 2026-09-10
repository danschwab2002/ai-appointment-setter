# Contrato — paquete HTML privado de feedback diario V1

- **Estado:** adquisición, minimización y materialización durable local implementadas; no autorizada para distribución
- **Versión:** `daily-feedback-review-package-v1`
- **Superficie:** recolección manual de Chatwoot → minimización → batch durable local → HTML privado de cuarentena
- **No incluye:** aplicación HTTPS autenticada, scheduler, Slack, persistencia remota, interpretación de feedback ni cambios de Conversation Release

## 1. Propósito

Este corte permite construir, para una ventana UTC cerrada, un único HTML navegable con las conversaciones elegibles de un inbox configurado. El HTML sirve únicamente como superficie local de cuarentena para validar adquisición, minimización y navegación. No es un adjunto Slack ni una autorización de acceso. No envía mensajes, no activa cambios y no da acceso a Chatwoot.

La interfaz es una superficie primaria **Operate / Inspect**: lista secuencial a la izquierda, conversación seleccionada al centro y decisión local al final. No es un dashboard ni un sitio público.

## 2. Autoridad de origen

El recolector recibe por configuración confiable:

- origen HTTPS de Chatwoot, sin credenciales, query, fragment ni path incorporados;
- `account_id` positivo;
- `inbox_id` positivo y único para el alcance;
- `agent_bot_id` positivo;
- token de lectura de Chatwoot;
- clave aleatoria de pseudonimización de exactamente 32 bytes, representada como 64 caracteres hexadecimales;
- `tenant_ref` y `scope_ref` internos;
- ventana UTC semiabierta `[window_start, window_end)`;
- linaje de Conversation Release fijado como `release_lineage_unavailable / 0` hasta que el runtime exponga una lectura canónica por conversación.

Los secretos sólo se leen desde variables de entorno y nunca se imprimen ni se incluyen en HTML o manifest.

La V1 consulta únicamente:

```text
GET /api/v1/accounts/{account_id}/conversations
  ?inbox_id={inbox_id}&status=all&page=N

GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
  ?before={message_id}
```

Cada conversación devuelta debe declarar exactamente el `inbox_id` configurado. Cualquier fila fuera del inbox falla cerrada antes de materializar el lote.

La lista debe incluir `data.payload` y `data.meta.current_page/all_count` coherentes. Ausencia, conteo contradictorio, página repetida, historial que no avanza o límite de páginas alcanzado producen error; no se publica un lote parcial como completo.

## 3. Selección elegible

Por conversación se consideran sólo mensajes que:

- caen dentro de la ventana UTC;
- son públicos (`private = false`);
- tienen contenido textual no vacío;
- son inbound de un contacto, o outbound del `agent_bot_id` exacto;
- si son outbound, tienen estado `sent`, `delivered` o `read`.

Se omiten notas privadas, eventos de sistema, adjuntos, mensajes humanos y mensajes outbound fallidos o ambiguos.

Una conversación es elegible sólo si contiene al menos un mensaje del prospecto y uno del agente en la ventana. El orden es estable por primer timestamp y luego por ID canónico; los IDs externos no salen del recolector.

## 4. Minimización y pseudonimización

Antes de renderizar, el sistema:

- reemplaza el nombre canónico del contacto y su primer nombre por `[NOMBRE]`;
- reemplaza correos por `[EMAIL]`;
- reemplaza teléfonos plausibles de siete o más dígitos por `[TELÉFONO]`;
- reemplaza URLs por `[ENLACE]`;
- reemplaza patrones explícitos de token, API key, bearer o secret por `[SECRETO REDACTADO]`;
- omite nombres de remitentes y metadatos de contacto;
- convierte IDs de conversación y mensaje en referencias HMAC opacas;
- no incluye account, inbox, conversation ID ni message ID externos en el HTML o manifest.

La sanitización automática no puede probar la ausencia de nombres libres, domicilios ni datos sensibles expresados en lenguaje natural. Por eso todo paquete nace con:

```json
{
  "human_reviewed": false,
  "distribution_ready": false
}
```

La revisión humana de privacidad se registra sobre el **hash exacto del HTML existente**, sin volver a consultar Chatwoot ni regenerar el contenido. Aun después de esa revisión, `distribution_ready` permanece en `false`: este corte no posee la autorización de negocio ni el servicio de acceso necesarios para distribución o Slack.

## 5. Artefactos

El output contiene sólo:

```text
<output-dir>/review.html
<output-dir>/manifest.json
```

La carpeta final usa modo `0700`; ambos archivos usan `0600`. El destino debe ser nuevo y no puede absorber contenido preexistente. HTML y manifest se construyen dentro de una carpeta privada temporal, se sincronizan y se publican juntos mediante un único rename del directorio; un fallo normal antes del rename no deja un artefacto visible en el destino.

El manifest no contiene transcripciones. Conserva:

- versión de schema;
- fingerprint del paquete;
- ventana, `created_at` y `expires_at`;
- cantidad de conversaciones;
- versiones de selección y sanitización;
- estado de revisión humana y distribución;
- SHA-256 del HTML;
- `activated_changes = 0`.

## 5.1. Materialización durable local

`materialize_daily_review_package(...)` revalida el schema, los gates de retención/cifrado, la versión exacta del sanitizer, mensajes y metadatos con el sanitizer determinístico compartido, y el linaje `release_lineage_unavailable / 0`. `DailyFeedbackBatchStore.create_minimized_review_batch(...)` repite el control de sanitización en el límite durable para impedir que un caller interno evada el materializador; la entrada pública nunca se convierte en un fixture.

El batch comprometido conserva tenant, scope, ventana, reviewer, binding revocable, expiración de retención, responsable de eliminación, versiones de selección/sanitización y `source_kind = canonical_minimized_conversation`. Cada snapshot conserva el transcript minimizado, referencias opacas de mensaje, autoría, timestamps, outcome y hash dentro del protocolo manifest → artifacts → commit existente. Replay exacto es idempotente; la misma clave lógica con contenido, autoridad o retención diferentes falla cerrada.

`get_review_batch_authority(batch_id)` sólo devuelve la autoridad de un batch real minimizado después de verificar manifest, commit y hashes. `get_next_review_item(...)` rechaza el transcript cuando alcanza `retention_expires_at`, incluso si el lease había sido adquirido antes. Estas lecturas preparan la reautorización HTTP posterior, pero todavía no constituyen una aplicación HTTPS ni una validación live del binding.

## 6. Propiedades del HTML

El HTML:

- es autocontenido y no carga imágenes, fuentes ni librerías remotas;
- declara CSP con `connect-src 'none'`, `img-src 'none'`, `object-src 'none'`, `frame-src 'none'` y `form-action 'none'`;
- escapa todo texto proveniente de conversaciones;
- permite navegar por conversación, avanzar y retroceder;
- permite elegir `correct`, `correct_with_feedback` o `skip`;
- conserva el feedback literal sólo en memoria del navegador mientras está abierto;
- permite descargar un JSON `daily-feedback-owner-decisions-v1`;
- fija `activated_changes = 0`.

El JSON descargado todavía no se importa automáticamente al store durable. El adaptador futuro deberá transformar cada decisión en los comandos existentes de `DailyFeedbackBatchStore`, validando reviewer binding, batch, item, revisión y fence. No podrá confiar sólo en el contenido del HTML.

## 7. Ejecución manual

Variables requeridas:

```text
CHATWOOT_BASE_URL
CHATWOOT_ACCOUNT_ID
CHATWOOT_INBOX_ID
CHATWOOT_AGENT_BOT_ID
CHATWOOT_API_ACCESS_TOKEN
DAILY_FEEDBACK_PSEUDONYMIZATION_KEY  # 64 hex
DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED  # debe ser exactamente true
DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED # debe ser exactamente true
DAILY_FEEDBACK_RETENTION_HOURS             # entero entre 1 y 168
DAILY_FEEDBACK_DELETION_OWNER              # responsable explícito
```

El recolector de conversaciones reales falla cerrado si cualquiera de los dos gates no es el booleano textual exacto `true`, si la retención no está entre 1 y 168 horas o si no existe responsable de eliminación. `STORAGE_ENCRYPTION_VERIFIED` es una atestación operacional: antes de habilitarla debe existir evidencia externa del volumen cifrado; el programa no puede inferir cifrado físico desde Python.

Comando:

```bash
uv run python scripts/build_daily_feedback_review.py \
  --tenant-ref <tenant-interno> \
  --scope-ref <alcance-interno> \
  --window-start 2026-09-09T00:00:00Z \
  --window-end 2026-09-10T00:00:00Z \
  --output-dir data/daily-feedback/2026-09-09
```

El comando de construcción siempre deja el archivo marcado **NO DISTRIBUIR**. Después de revisar manualmente esos bytes, la revisión de privacidad se registra sin recollectar mediante:

```bash
uv run python scripts/approve_daily_feedback_review.py \
  --bundle-dir data/daily-feedback/2026-09-09 \
  --expected-html-sha256 <hash-del-manifest> \
  --reviewer-binding-ref <binding-opaco>
```

Esto cambia `human_reviewed` pero conserva `distribution_ready = false`.

`purge_expired_review_bundle(...)` elimina únicamente un bundle cuyo `expires_at` ya venció, después de comprobar inventario exacto, propietario, modos `0700/0600`, ausencia de symlinks/hardlinks y coincidencia del hash HTML. El scheduler que invoque esta función sigue pendiente; hasta entonces el responsable configurado debe ejecutarla operacionalmente.

## 8. Frontera con Slack

El conector Slack futuro **no debe adjuntar este HTML local directamente**. Debe publicar un enlace HTTPS opaco a una aplicación que, en cada `GET`, reautorice tenant, alcance, batch comprometido y binding revocable del revisor. Antes de habilitar ese enlace, otra capa deberá:

- leer el contenido minimizado desde el batch comprometido e íntegro de `DailyFeedbackBatchStore` o su reemplazo productivo;
- reautorizar tenant, alcance, revisor, binding, batch e ítems desde estado canónico, no desde parámetros del HTML;
- servir con `Cache-Control: private, no-store`, `Referrer-Policy: no-referrer` y `X-Content-Type-Options: nosniff`;
- exigir que el hash del HTML coincida con el snapshot autorizado;
- usar una clave de entrega estable y ledger durable;
- fijar workspace/canal exactos, `unfurl_links=false` y `unfurl_media=false`;
- reconciliar resultados ambiguos sin reenvío ciego.

Nunca debe enviar rutas `file://`, URLs bearer de larga duración, secretos en query strings ni nombres de archivo con PII. Si en el futuro se evalúa subir el HTML a Slack, antes deben existir retención, borrado remoto, restricciones de sharing y reconciliación de esa copia adicional.

## 9. Limitaciones explícitas de la V1

1. La ejecución es manual y local; no existe scheduler ni servicio HTTPS autorizado.
2. `tenant_ref` y `scope_ref` siguen siendo afirmaciones del operador. El linaje no se inventa: permanece `release_lineage_unavailable / 0` hasta obtenerse del runtime canónico. Por eso ningún resultado de este corte es distribuible.
3. La sanitización automática requiere revisión humana, pero esa revisión sólo habilita evaluación local.
4. No hay política productiva de retención ni eliminación configurada todavía; sólo existen gates y una primitiva de purga manual.
5. El HTML exporta decisiones, pero todavía no las envía al workflow durable; el store ya puede conservarlas cuando una futura API autorizada invoque sus comandos.
6. No existe aún autorización de reviewer/channel ni transporte Slack para este flujo.
7. El recolector valida la cobertura declarada de conversaciones y pagina cada historial hasta observar una página vacía o cruzar el inicio de la ventana; una página corta no se considera evidencia de completitud. La estrategia sigue limitada al volumen bajo del MVP y al contrato observado de cursor `before`.
8. El batch íntegro local ya existe; la promoción a distribución exige persistencia compartida y una API de negocio que reautorice cada acceso, todavía no implementadas.

Ninguna de estas limitaciones autoriza a representar el corte como activo en producción.
