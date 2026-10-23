# La app de Slack del Automation Expert

El Automation Expert (el profile `automation-expert`, ADR-0025 y `hermes/README.md`) le habla a la
gente por mensaje directo de Slack, con Socket Mode: no tiene URL pública ni API server. Cada
instancia crea su propia app desde el mismo manifiesto,
[`deploy/slack-app-manifest-automation-expert-v1.json`](../../deploy/slack-app-manifest-automation-expert-v1.json),
y carga sus dos tokens con `despliegue/hermes/cargar-automation-expert.py` del repo de la instancia.

Este documento dice cómo se crea la app y de dónde sale cada permiso. No afirma que una instancia la
tenga instalada: eso lo dice la evidencia de cada instalación.

## Crear la app

1. En <https://api.slack.com/apps>: **Create New App** → **From an app manifest** → el workspace de
   la instancia → pegar el JSON del manifiesto, tal cual → **Create**.
2. **Install App** → **Install to Workspace** → **Allow**. Queda el **Bot User OAuth Token**
   (`xoxb-…`).
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**, con el scope
   `connections:write`. Queda el **App-Level Token** (`xapp-…`). Socket Mode ya viene prendido por el
   manifiesto; sin este token no conecta.
4. En el VPS, con terminal (`ssh -t`): `cargar-automation-expert.py` de la instancia pide los dos
   tokens sin mostrarlos y los IDs de Slack de quienes le pueden escribir, y los prueba contra Slack
   (`auth.test` y `apps.connections.open`) antes de escribir nada.

Si más adelante cambia un scope o un evento, Slack pide reinstalar la app, y el bot token puede
cambiar: se vuelve a correr el paso 4.

## De dónde sale cada cosa

La fuente es Hermes 0.21.0 (`NousResearch/hermes-agent` en `29112be`, la imagen que fija
`hermes/declaracion.toml`), leída el 2026-10-23. El punto de partida es el manifiesto que genera el
mismo Hermes (`hermes slack manifest`, `hermes_cli/slack_cli.py:79-160`), recortado a lo que usa un
profile que atiende **solo mensajes directos 1:1** (`profiles/automation-expert/config.yaml`:
`allowed_channels` con un centinela, `allow_bots: none`).

Los scopes del bot, cada uno con lo que lo usa en `plugins/platforms/slack/adapter.py`:

- `chat:write`: contestar y editar (`chat_postMessage`, `chat_update`, `chat_delete`,
  `chat_postEphemeral` y los streams de respuesta).
- `commands`: los slash commands de abajo.
- `files:write`: subir archivos con `files_upload_v2`. Lo usa el lienzo de `draw_flow` (el plugin
  devuelve un `MEDIA:` y Hermes lo adjunta); el CHANGELOG lo pide desde la v1.16.0 (#295).
- `im:history`: recibir el evento `message.im` y leer el hilo del DM (`conversations_replies`).
- `im:read`: `conversations_info` de un canal `D`.
- `im:write`: abrir el DM a partir de un ID de usuario (`conversations_open`, en
  `_ensure_dm_conversation`).
- `reactions:write`: los ojos mientras procesa y el tilde o la cruz al terminar
  (`on_processing_start` y `on_processing_complete`, prendidos por defecto con `SLACK_REACTIONS`).
  El manifiesto de Hermes no lo pide, y sin él la reacción falla en silencio.
- `users:read`: el nombre de quien escribe (`users_info`, en `_resolve_user_name`).

Lo que el manifiesto de Hermes pide y este no, porque el profile no lo usa:

- `channels:*`, `groups:*`, `mpim:*`, `app_mentions:read` y sus eventos (`message.channels`,
  `message.groups`, `message.mpim`, `app_mention`): el Automation Expert no atiende canales ni DMs de
  grupo.
- `files:read`: leer archivos que alguien le manda. El profile tiene apagados los toolsets de archivos
  y de visión; un adjunto llega sin su contenido y el texto se contesta igual.
- `reactions:read`: las reacciones como señal de entrada no se usan.
- `assistant:write` y la vista de asistente (`assistant_view`): el DM queda plano, como con
  `hermes slack manifest --no-assistant`.

Los slash commands salen de `slack_native_slashes()` (`hermes_cli/commands.py:1496-1575`), con la
descripción de `COMMAND_REGISTRY`: `/hermes` (el que Hermes reserva para todo lo demás), `/new` y
`/stop`, que el profile deja a cualquiera (`user_allowed_commands`), y `/model` y `/restart`, que
solo corren quienes administran (`administran` en el `hermes.toml` de la instancia). `/status` no va:
choca con un comando propio de Slack, y se llega por `/hermes status`. En Socket Mode Slack ignora la
`url` de cada comando; va el marcador de Hermes.

La interactividad queda prendida, como en el manifiesto de Hermes: en Socket Mode los botones llegan
por la misma conexión y no hace falta URL.

## Cómo se prueba

- `tests/test_automation_expert_slack_manifest.py`: el manifiesto pide exactamente esos scopes y ese
  evento, Socket Mode prendido, ninguna URL de una instancia, los slash commands cubren los comandos
  del profile y el nombre no nombra ninguna instancia.
- En cada instalación, el día 0: crear la app desde el manifiesto, cargar los tokens con
  `cargar-automation-expert.py`, prender `AUTOMATION_EXPERT_READ_ENABLED` en el `bridge.env` (sin
  ese flag el bridge no crea las rutas y el plugin contesta error), instalar el profile con
  `scripts/hermes_instancia.py aplicar` y pedirle al Automation Expert, por mensaje directo, la lista
  de flujos (`list_flows`, que llama a `GET /internal/automation-expert/v1/flows`). En una instancia
  recién instalada tiene que contestar que no hay flujos.
