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
- Los slash commands (`slash_commands`, de `slack_native_slashes()`) y el scope `commands` que los
  habilita. Ver la sección que sigue.

## Sin slash commands

El manifiesto no registra ningún slash command, a propósito. Slack le da cada comando a la última app
que lo instaló en todo el espacio de trabajo, no a la del mensaje directo. El 28/10, al instalar la app
de Maribel en el Slack de Lancemos, se quedó con el `/model` de otro agente de Hermes que ya vivía ahí;
hubo que borrar a mano los cinco comandos de la app instalada (`/hermes`, `/new`, `/stop`, `/model` y
`/restart`). Con este manifiesto, una instalación nueva no le saca nada a nadie.

Ningún código del producto usaba esos comandos: el Automation Expert trabaja por mensaje directo y el
botón de aprobación llega por la interactividad, que sigue prendida. Los comandos del profile se
escriben en el mismo DM con `!` en vez de `/`: `!new`, `!stop` y `!status`, que el profile deja a
cualquiera (`user_allowed_commands`), y `!model` y `!restart`, que solo corren quienes administran
(`administran` en el `hermes.toml` de la instancia). El adaptador de Slack de Hermes pasa el `!` a `/`
cuando la primera palabra es un comando conocido (`_rewrite_known_bang_command`,
`plugins/platforms/slack/adapter.py:445-459`), y el mismo control de acceso de `gateway/run.py` decide
quién lo corre. Un texto como «!buenísimo» pasa como mensaje normal.

Una app ya instalada con los comandos se arregla en **Slash Commands** (borrar los cinco) y después
**OAuth & Permissions** (sacar `commands`), y Slack pide reinstalar: se vuelve a correr el paso 4.

La interactividad queda prendida, como en el manifiesto de Hermes: en Socket Mode los botones llegan
por la misma conexión y no hace falta URL.

## Cómo se prueba

- `tests/test_automation_expert_slack_manifest.py`: el manifiesto pide exactamente esos scopes y ese
  evento, Socket Mode prendido, ninguna URL de una instancia, ningún slash command ni el scope
  `commands`, y el nombre no nombra ninguna instancia.
- En cada instalación, el día 0: crear la app desde el manifiesto, cargar los tokens con
  `cargar-automation-expert.py`, prender `AUTOMATION_EXPERT_READ_ENABLED` en el `bridge.env` (sin
  ese flag el bridge no crea las rutas y el plugin contesta error), instalar el profile con
  `scripts/hermes_instancia.py aplicar` y pedirle al Automation Expert, por mensaje directo, la lista
  de flujos (`list_flows`, que llama a `GET /internal/automation-expert/v1/flows`). En una instancia
  recién instalada tiene que contestar que no hay flujos.
