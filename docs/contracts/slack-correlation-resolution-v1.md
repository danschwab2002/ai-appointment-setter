# Contrato de resolución de correlaciones en Slack V1

- **Estado:** implementación local en curso; producción default-off
- **Fecha:** 2026-09-09
- **Autoridad:** Supabase Cloud mediante el bridge scoped del aliado
- **Superficie:** modal nativo de Slack y actualización del mensaje raíz

## 1. Routing y aislamiento

La app de Slack puede ser compartida, los canales no. Cada tenant autenticado se
resuelve mediante `SLACK_TENANT_CHANNELS_JSON`:

- `johanna` → `C0C0YEACVT2`;
- `att1` → otro Channel ID, todavía pendiente de creación.

El nombre visible del canal no concede identidad. El Channel ID es server-owned,
dos tenants no pueden compartirlo y no existe fallback. Hasta que ATT1 tenga su canal, no tiene token productor ni ruta y cualquier
admisión ATT1 falla `401` antes de leer o persistir el cuerpo.

La allowlist de resolutores también es por tenant. Sólo un Slack User ID exacto
de `SLACK_TENANT_OPERATOR_USER_IDS_JSON` puede abrir o confirmar un caso de ese
canal. Nombres visibles, menciones, pertenencia al canal y roles de Slack no
conceden autoridad.

## 2. Mensaje accionable

`COR-001`, `COR-002` y `COR-003` incluyen un botón `Revisar caso`. El mensaje
conserva únicamente datos enmascarados, conteos, códigos cerrados y referencias
opacas. El `case_id` del botón nunca es autoridad suficiente: el conector exige
que coincida con un registro durable aceptado cuyos Team ID, Channel ID,
`message_ts`, tenant y `subject_ref` también coincidan.

Los demás códigos del catálogo permanecen sin acciones.

## 3. Endpoint Slack

### `POST /slack/interactions`

Sólo existe con `SLACK_INTERACTIONS_ENABLED=true`. Requiere:

```http
Content-Type: application/x-www-form-urlencoded
X-Slack-Request-Timestamp: <epoch>
X-Slack-Signature: v0=<hmac-sha256>
```

La firma se verifica sobre los bytes crudos antes de parsear. La ventana máxima
es ±300 segundos. El body es acotado y sólo admite el campo `payload`. Los campos
autoritativos (team, canal, `message.ts`, acción/callback, usuario y token de
sesión) se validan por tipo y binding exactos; los campos Slack no autoritativos
admiten extensiones únicamente si pasan el validador recursivo acotado de claves,
profundidad, cardinalidad, longitud y caracteres de control.

Antes de reservar replay se ejecuta un precheck local, sin red ni mutaciones, de
shape, allowlist y binding. Un click/submit bien formado pero no autorizado recibe
`200 {}` y no crea replay, sesión ni llamada remota. Para un request autorizado,
la reserva de fingerprint y la admisión del trabajo ocurren en una sola transacción
SQLite: nunca queda replay reservado sin su transición durable correspondiente.

`block_actions` crea en esa transacción la sesión y un job de apertura que contiene
el `trigger_id` efímero, y responde `200 {}` sin esperar `get_case` ni `views.open`.
El worker relee el caso, persiste `request_started` justo antes de `views.open` y
vacía el trigger al terminar. Un open `request_started` interrumpido o ambiguo se
marca `failed` al reiniciar y nunca se reintenta a ciegas; el operador debe pulsar
de nuevo.

Los submits persisten primero la transición `preparing` o `confirming` y responden
de inmediato con `response_action=update` y un modal local `Procesando…`, de modo
que Slack no cierre la vista. Un worker fuera del request ejecuta `prepare`/`confirm`
con la identidad e idempotency key durables y reemplaza esa vista mediante
`views.update` por confirmación, éxito o error seguro. El `view.id` y `view.hash`
recibidos quedan persistidos; el update posterior omite el hash ya obsoleto tras
el update síncrono. Un reinicio reanuda esos estados sin depender de tareas en
memoria.

La poda se ejecuta sólo después de autorizar. Los replays respondidos se conservan
como máximo 24 horas y 10.000 filas. Las sesiones terminales se conservan 30 días
dentro de un máximo de 10.000 filas; una sesión `opened` sólo se elimina al vencer
su expiración segura. Nunca se poda trabajo `preparing`, `prepared` o `confirming`;
si el trabajo activo consume la capacidad, una nueva admisión falla cerrada en vez
de eliminarlo.

## 4. Apertura y sesión de revisión

La acción `review_operator_correlation`:

1. verifica firma;
2. ejecuta precheck local de Team ID, canal exclusivo, mensaje aceptado y usuario allowlisted;
3. deriva el tenant y caso desde el binding durable, no desde el botón;
4. admite atómicamente replay + sesión + job de apertura y responde `{}`;
5. el worker relee el caso enmascarado y sus candidatos desde el bridge;
6. el worker marca request-start y abre el modal mediante `views.open`.

La sesión expira y no puede transferirse a otro usuario, workspace, canal,
mensaje o tenant.

## 5. Prepare y confirm

El primer submit `prepare_operator_correlation_resolution` acepta únicamente una
opción proyectada y un fundamento cerrado. El conector genera una idempotency key
durable y llama:

```text
POST /internal/operator/correlations/resolutions/prepare
```

La respuesta reemplaza el modal por una confirmación explícita. Todavía no existe
resolución terminal.

El segundo submit `confirm_operator_correlation_resolution` usa exclusivamente el
comando ya persistido; no toma acción ni candidato nuevos desde Slack. Llama:

```text
POST /internal/operator/correlations/resolutions/confirm
```

El bridge vuelve a verificar scope server-owned, expiración, candidato,
evidencia, concurrencia e idempotencia. Evidencia obsoleta, caso ya resuelto o
comando vencido fallan cerrado.

## 6. Resultado y proyección

Supabase es autoritativo. Tras una confirmación aplicada, el conector actualiza el
mismo mensaje raíz mediante `chat.update`, retira los botones y muestra:

- `Resuelto — candidato vinculado`, o
- `Cerrado — sin coincidencia válida`.

También muestra el Slack User ID del operador y el timestamp autoritativo, sin
PII ni comentarios libres.

La actualización tiene ledger durable:

```text
pending → claimed → request_started → accepted
                                  ↘ rejected
                                  ↘ delivery_unknown
```

Un timeout o identidad de respuesta no verificable después de `request_started`
no se reintenta a ciegas. La resolución de Supabase no se revierte; la proyección
queda señalada para reconciliación operator-only.

## 7. Reconciliación de mensajes existentes

Las notificaciones `COR-*` aceptadas antes de habilitar interactividad se
actualizan en lugar de repostearse. El proceso:

1. selecciona como máximo un binding aceptado por ejecución;
2. conserva su tenant, canal y `message_ts` originales;
3. relee el caso enmascarado;
4. persiste `request_started`;
5. llama `chat.update` sobre el mismo mensaje;
6. finaliza `accepted`, `rejected` o `delivery_unknown`.

La ruta operator-only requiere `SLACK_OPERATOR_BEARER_TOKEN` y el flag separado
`SLACK_CORRELATION_BACKFILL_ENABLED=true`. Puede ejecutarse con
`SLACK_INTERACTIONS_ENABLED=false`; no usa `chat.postMessage` y nunca mueve un
mensaje a otro canal. Una caída en `claimed` vuelve a `pending`; sólo una caída
después de persistir `request_started` queda `delivery_unknown`.

## 8. Configuración de activación

Interactividad requiere, como mínimo:

- `SLACK_INTERACTIONS_ENABLED=true`;
- `SLACK_SIGNING_SECRET`;
- `SLACK_TEAM_ID`;
- `SLACK_BOT_TOKEN`;
- `SLACK_TENANT_CHANNELS_JSON`;
- `SLACK_TENANT_OPERATOR_USER_IDS_JSON`;
- `SLACK_TENANT_OPERATOR_BACKENDS_JSON` con base URL HTTPS y tokens read/write
  diferentes para cada tenant habilitado.

La Slack App debe usar como Request URL:

```text
https://infra-supportmagician-slack-connector.u5iqmf.easypanel.host/slack/interactions
```

La presencia de secretos no habilita interactividad. El flag permanece en
`false` durante el primer deploy y se activa sólo después de health/readiness,
smoke firmado y verificación del operador.

## 9. Criterio E2E

No se considera terminado hasta que un operador allowlisted resuelva un caso
real desde Slack y se verifique físicamente:

1. modal abierto desde el mensaje de Johanna;
2. prepare persistido sin resolver;
3. confirm aplicado una sola vez en Supabase;
4. auditoría de actor y fecha;
5. mensaje original actualizado sin duplicados;
6. cero cambios en otro tenant/canal;
7. `/ready` saludable y ledgers sin estados inciertos.
