# Despliegue del conector central de Slack en EasyPanel

- **Estado:** Procedimiento operativo listo; despliegue pendiente
- **Fecha:** 2026-09-07
- **Servicio:** `supportmagician-slack-connector`
- **Dockerfile:** `deploy/slack-connector.Dockerfile`
- **Contrato:** [Slack Operations Connector V1](../contracts/slack-operations-connector-v1.md)

## 1. Crear el servicio

Después de mergear la implementación a la rama que usa EasyPanel:

1. Crear una aplicación nueva llamada `supportmagician-slack-connector` desde el
   mismo repositorio.
2. Elegir build por Dockerfile y configurar la ruta exacta
   `deploy/slack-connector.Dockerfile`.
3. Exponer el puerto interno `8000`.
4. Configurar exactamente **una réplica**. No usar autoscaling.
5. Crear un volumen persistente y montarlo exactamente en `/app/data`.
6. Asignar un dominio HTTPS accesible desde los bridges. No activar todavía
   ingreso ni publicación.

No reutilizar el servicio `johanna` ni `att1`; éste es un tercer runtime.

## 2. Primer arranque inerte

Configurar:

```text
SLACK_INGRESS_ENABLED=false
SLACK_NOTIFICATIONS_ENABLED=false
SLACK_INTERACTIONS_ENABLED=false
SLACK_CONNECTIVITY_CHECK_ENABLED=false
SLACK_STORAGE_PATH=/app/data/slack-connector.sqlite3
SLACK_WORKER_ID=slack-worker-1
SLACK_POLL_INTERVAL_SECONDS=1
SLACK_MAX_NONTERMINAL_NOTIFICATIONS=10000
```

Desplegar y verificar:

```text
GET https://<host>/health → 200 {"status":"ok"}
GET https://<host>/ready  → 200, mode=inactive
```

Si el volumen falta o no es escribible, no continuar.

## 3. Cargar configuración privada

Crear en el gestor de secretos dos valores aleatorios distintos de al menos 32
caracteres. No copiarlos en Git, documentación, comandos con valor literal ni
chat.

En el conector, cargar directamente por la UI de EasyPanel:

```text
SLACK_BOT_TOKEN=<bot token de SupportMagician Ops>
SLACK_TEAM_ID=<workspace Team ID esperado>
SLACK_CHANNEL_ID=C0C0YEACVT2
SLACK_TENANT_TOKENS_JSON={"johanna":"<token johanna>","att1":"<token att1>"}
```

En cada bridge, conservar únicamente su propio token interno y la URL HTTPS del
conector. Ningún bridge recibe `SLACK_BOT_TOKEN`.

Mantener todos los flags en `false`, redeployar y confirmar que `/ready` no
expone valores: sólo booleanos y conteos sanitizados.

## 4. Verificar identidad Slack sin publicar

Cambiar únicamente:

```text
SLACK_CONNECTIVITY_CHECK_ENABLED=true
```

Redeployar. Exigir:

```text
GET /ready → 200, mode=connectivity_verified
```

Un `503 connectivity_failed` detiene el rollout. Corregir token o Team ID; no
activar publicación.

## 5. Probar admisión durable de ambos bridges

Cambiar:

```text
SLACK_INGRESS_ENABLED=true
SLACK_NOTIFICATIONS_ENABLED=false
```

Redeployar. `/ready` debe devolver `200 admission_only`.

Desde el terminal de cada bridge, usar su token ya cargado como variable de
entorno; no escribir su valor en la línea de comando. Enviar un evento sintético
por bridge con un UUID y SHA-256 fijos distintos. El cuerpo no debe contener
nombres, teléfonos, emails, JIDs ni texto real.

Resultados exigidos:

- primera admisión: HTTP `202`, `delivery_state=pending`;
- replay exacto: HTTP `200`, `status=duplicate`;
- mismo identificador con otra semántica: HTTP `409`;
- token del otro bridge: no permite leer el evento (`404`);
- Slack todavía contiene cero mensajes nuevos.

## 6. Primera publicación controlada

Con las dos admisiones sintéticas pendientes, cambiar únicamente:

```text
SLACK_NOTIFICATIONS_ENABLED=true
```

Redeployar. Exigir `/ready → 200, mode=operational`. El worker debe publicar
exactamente dos avisos sanitizados —uno etiquetado Johanna y otro ATT1— y sus
estados deben llegar a `accepted`.

Comprobar en Slack:

- app `SupportMagician Ops`;
- canal `C0C0YEACVT2`;
- exactamente un mensaje por `event_id/dedupe_key`;
- sin PII, texto de clientes ni payloads crudos;
- el replay exacto no crea un tercer mensaje.

Un estado `delivery_unknown` no se reintenta. Se reconcilia manualmente contra
el canal antes de cualquier sucesor.

## 7. Activación de productores

Cada bridge usa `SlackConnectorProducer` con:

```text
SLACK_CONNECTOR_BASE_URL=https://<host>
SLACK_CONNECTOR_BEARER_TOKEN=<sólo su token>
```

El productor debe construir `event_id`, `event_code` y `dedupe_key` desde el
evento durable autoritativo. Reintentar una admisión de resultado incierto exige
el comando exacto; cambiar semántica bajo el mismo identificador falla `409`.

No se admite llamar al conector con texto, bloques, canal o `tenant_ref`.

## 8. Rollback

1. Poner `SLACK_INGRESS_ENABLED=false` para detener nuevas admisiones.
2. Si debe detenerse publicación, poner `SLACK_NOTIFICATIONS_ENABLED=false`.
3. No borrar ni desmontar `/app/data`; preserva pending y resultados inciertos.
4. Mantener una réplica.
5. Revertir la imagen sólo si conserva compatibilidad con el mismo schema del
   ledger.

## 9. Interactividad

Mantener apagados **Interactivity & Shortcuts**, **Event Subscriptions**,
**Incoming Webhooks** y **Socket Mode**. V1 implementa avisos outbound; no finge
resolución por botones o modales.
