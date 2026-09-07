# Conexión inicial de la app operativa de Slack

- **Estado:** Runbook propuesto; fase de control plane lista para ejecución manual
- **Fecha:** 2026-09-07
- **Alcance:** crear, instalar y vincular una app Slack con privilegio mínimo
- **No implementa:** endpoints interactivos, outbox, persistencia, workers, deploy ni activación de avisos reales
- **Manifest:** [`deploy/slack-app-manifest-v1.json`](../../deploy/slack-app-manifest-v1.json)

## 1. Arquitectura elegida

```text
Supabase Cloud / bridge
→ outbox durable y worker default-off
→ Slack Web API (`chat.postMessage` / `chat.update`)
→ canal operativo compartido

botón de Slack
→ HTTPS público del bridge
→ verificación HMAC sobre bytes crudos
→ autorización de workspace + canal + usuario
→ lectura fresca de Supabase Cloud
→ `views.open` / prepare / confirm
```

Para V1 se usa una **Slack App con bot token**, no un Incoming Webhook:

- necesitamos publicar y actualizar mensajes;
- necesitamos botones y modales nativos;
- necesitamos validar la firma de cada interacción;
- el canal es único y el bot será invitado explícitamente;
- no necesitamos leer mensajes del canal, recibir Events API ni usar Socket Mode.

Permiso inicial: sólo `chat:write`. No se solicita `chat:write.public` porque el
bot se invita al canal. Tampoco se solicitan scopes de lectura de conversaciones,
usuarios o archivos.

## 2. Fase A — crear e instalar la app

Esta fase no produce mensajes ni conecta todavía el bridge.

1. Abrir <https://api.slack.com/apps>.
2. Seleccionar **Create New App**.
3. Elegir **From an app manifest**.
4. Seleccionar el workspace que contiene el canal operativo.
5. Elegir formato **JSON**.
6. Copiar el contenido exacto de `deploy/slack-app-manifest-v1.json`.
7. Revisar que Slack muestre únicamente el bot scope `chat:write`.
8. Crear la app.
9. Entrar en **OAuth & Permissions** y seleccionar **Install to Workspace**.
10. Autorizar la instalación.
11. En Slack, abrir el canal operativo e invitar la app:

```text
/invite @SupportMagician Ops
```

No activar todavía **Interactivity & Shortcuts**, **Event Subscriptions**,
**Incoming Webhooks** ni **Socket Mode**.

## 3. Datos que produce la fase A

Conservar fuera de Git:

| Dato | Ubicación en Slack | Tratamiento |
|---|---|---|
| App ID | **Basic Information → App Credentials** | configuración privada |
| Signing Secret | **Basic Information → App Credentials** | secreto; nunca copiar al chat o Git |
| Bot User OAuth Token (`xoxb-…`) | **OAuth & Permissions** | secreto; nunca copiar al chat o Git |
| Workspace/Team ID | workspace o respuesta `auth.test` | configuración privada server-owned |
| Channel ID | detalles del canal → **About** | configuración privada server-owned |
| IDs de operadores permitidos | perfiles Slack autorizados | allowlist privada server-owned |
| User group ID de escalamiento | grupo operativo, si se usa | configuración privada server-owned |

No pegar tokens o signing secrets en esta conversación. Cuando el runtime esté
listo, cargarlos directamente en el secret store de EasyPanel.

Nombres de configuración previstos:

```text
SLACK_NOTIFICATIONS_ENABLED=false
SLACK_INTERACTIONS_ENABLED=false
SLACK_BOT_TOKEN=<secret>
SLACK_SIGNING_SECRET=<secret>
SLACK_TEAM_ID=<server-owned>
SLACK_CHANNEL_ID=<server-owned>
SLACK_ALLOWED_USER_IDS=<server-owned allowlist>
SLACK_ESCALATION_USERGROUP_ID=<optional server-owned>
```

Tener credenciales presentes no activa efectos. Los dos flags permanecen
`false` hasta sus pruebas controladas independientes.

## 4. Fase B — prueba física de publicación

Después de cargar los datos privados se hará una prueba de un solo mensaje:

1. llamar `auth.test` y exigir que `team_id` coincida con `SLACK_TEAM_ID`;
2. comprobar que el bot pertenece al canal configurado;
3. mantener outbox y workers apagados;
4. armar un presupuesto de exactamente un mensaje de prueba;
5. publicar un aviso sanitizado sin caso real;
6. validar en la respuesta `ok=true`, `channel=SLACK_CHANNEL_ID` y `ts` válido;
7. verificar visualmente un único mensaje en el canal;
8. conservar sólo evidencia sanitizada: IDs opacos, timestamp y resultado.

Texto propuesto para la prueba:

```text
🧪 Prueba de conexión
SupportMagician Ops puede publicar en este canal.
No contiene datos de clientes y no activa ninguna automatización.
```

Un timeout posterior al inicio del request se trata como resultado incierto. No
se repite la publicación hasta reconciliar el canal.

## 5. Fase C — habilitar botones y modales

Sólo después de desplegar y verificar el endpoint HTTPS:

1. abrir **Interactivity & Shortcuts**;
2. activar **Interactivity**;
3. configurar como Request URL:

```text
https://<bridge-host>/integrations/slack/interactions
```

4. guardar la configuración;
5. mantener **Event Subscriptions** y **Socket Mode** apagados.

Cada request debe verificarse antes de parsear el formulario:

- `X-Slack-Request-Timestamp` dentro de 300 segundos;
- `X-Slack-Signature` HMAC-SHA256 sobre `v0:{timestamp}:{raw_body}`;
- workspace/team exacto;
- canal y mensaje raíz registrados para el caso;
- usuario en allowlist activa;
- action/callback ID cerrado;
- caso y snapshot frescos desde Supabase Cloud.

El `trigger_id` sólo se usa para abrir el modal después de autenticar y autorizar
la interacción. Nunca concede scope por sí mismo.

## 6. Fase D — correlación real controlada

La primera activación funcional será una sola correlación sintética o controlada:

1. outbox elegible y presupuesto previos en cero;
2. un caso scoped `unmatched`, `ambiguous` o `conflict`;
3. un único mensaje raíz;
4. botón **Revisar caso**;
5. modal con datos enmascarados;
6. prepare y confirm separados;
7. mensaje raíz actualizado, no duplicado;
8. automatización todavía bloqueada;
9. rollback a flags `false`.

No se activarán simultáneamente las 49 plantillas. Primero se valida
`COR-001/COR-002/COR-003`; luego handoffs; después alertas sistémicas.

## 7. Criterio de finalización

La conexión inicial sólo queda verificada cuando existe evidencia de:

- app instalada en el workspace correcto;
- bot invitado al canal correcto;
- scope efectivo mínimo;
- `auth.test` coincidente;
- exactamente un mensaje controlado visible;
- cero PII y cero duplicados;
- flags funcionales otra vez apagados después del probe;
- secretos ausentes de Git, logs y documentación.

La creación de la app por sí sola no demuestra integración operativa.
