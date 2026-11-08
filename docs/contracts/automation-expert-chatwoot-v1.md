# automation-expert-chatwoot-v1: la herramienta acotada de Chatwoot del Automation Expert

Escrito al construir el PR de F-143 (E-256, 2026-11-01), antes del merge: el bridge contesta `automation-expert-chatwoot-v1` `1.1.0` con `AGENT_CAMPAIGNS_ENABLED` prendido (crea el macro de una campaña; ver `agent-campaigns-v1`) y `1.0.0` sin ese flag (`CONTRACT_VERSION` y `CONTRACT_VERSION_WITHOUT_MACRO_CREATION`, `src/bridge/automation_expert_chatwoot.py`). Describe la interfaz; no dice que nada esté desplegado ni activado.

## Por qué

Decisión de Dan del 31/10 (`2026-10-31-el-ae-escribe-en-chatwoot-con-una-herramienta-acotada`, en el vault): el agente crea y asigna etiquetas en Chatwoot, y lee lo que necesita, para que ningún cliente tenga que tocar Chatwoot a mano (el caso que la disparó: la etiqueta `cliente` que usa la bienvenida). **Lectura y creación sí; borrado no.** Lo que protege no es la aprobación (una etiqueta llega a Chatwoot igual que la armó el agente) sino el alcance de la herramienta: qué rutas y qué métodos acepta. El agente lee texto que no es nuestro (los historiales de los leads); una herramienta genérica sobre la API dejaría que un texto manipulado lo lleve a mandar mensajes, borrar o reasignar.

## La lista cerrada

Chatwoot no da tokens con alcance fino: la clave de control (`CHATWOOT_CONTROL_API_ACCESS_TOKEN`) es de un usuario administrador, y hoy de las cuentas 1, 2 y 3. Por eso el alcance vive en el bridge, en `ALLOWED_CALLS`:

| método | ruta de Chatwoot |
|---|---|
| `GET` | `/api/v1/accounts/{account}/labels` |
| `POST` | `/api/v1/accounts/{account}/labels` |
| `GET` | `/api/v1/accounts/{account}/conversations/{conversation}` |
| `GET` | `/api/v1/accounts/{account}/conversations/{conversation}/labels` |
| `POST` | `/api/v1/accounts/{account}/conversations/{conversation}/labels` |
| `GET` | `/api/v1/accounts/{account}/macros` |
| `POST` | `/api/v1/accounts/{account}/macros` (solo con `AGENT_CAMPAIGNS_ENABLED`; sin ese flag queda fuera de la lista) |

- `{account}` es siempre la cuenta del manifiesto (`chatwoot.account_id`), fija en el patrón: una ruta de otra cuenta que la clave también administra se rechaza. El bridge no arranca con el flag si `CHATWOOT_ACCOUNT_ID` no es la del manifiesto.
- `{conversation}` es un entero positivo sin ceros adelante. La ruta tiene que coincidir entera: sin query, sin `..`, sin barra final.
- **Todo `DELETE` se rechaza** (`chatwoot_delete_not_allowed`), en cualquier ruta, antes de salir a la red. Cualquier otra combinación, `chatwoot_call_not_allowed`. Sumar una ruta a la lista es una decisión, no un arreglo.
- No hay `PATCH`/`PUT` de una etiqueta, ni cambiar o ejecutar un macro, ni mensajes, asignaciones o estados de conversación. Crear un macro es solo el de una campaña, con `AGENT_CAMPAIGNS_ENABLED`, y solo uno que suma una etiqueta.

## Las rutas del bridge

Existen solo con `AUTOMATION_EXPERT_CHATWOOT_ENABLED=true` (apagado por defecto; exige `AUTOMATION_EXPERT_CHANGE_ENABLED`, la clave de control y el manifiesto). Piden la clave de cambio del agente (`AUTOMATION_EXPERT_CHANGE_TOKEN`); la de lectura no las abre. La clave de Chatwoot no sale del bridge. `/ready` suma `automation_expert_chatwoot: enabled`.

- **`GET /internal/automation-expert/v1/chatwoot/labels`**: `labels[]` (`id`, `title`, `description`, `color`, `show_on_sidebar`), `total`, `truncated` (más de 200).
- **`POST /internal/automation-expert/v1/chatwoot/labels`** con `{title, description?, color?, show_on_sidebar?, requested_by: {slack_user}}`. El título se pasa a minúsculas (como Chatwoot) y es de 1 a 40 letras, números, guion o guion bajo; uno que parece un teléfono o un mail, `identifier_not_allowed`; uno que empieza con `automation_` (la pausa y la baja del bridge), `chatwoot_label_reserved`. La descripción, hasta 200 caracteres, sin teléfonos ni mails. El color, `#rrggbb` (default `#1f93ff`, el de Chatwoot). Lee las etiquetas primero: si ya existe, `200` con `created: false` y no escribe; si no, la crea y contesta `201` con `created: true`. Si Chatwoot contesta `422` porque otro la creó entre la lectura y la escritura, relee y contesta `created: false`.
- **`POST /internal/automation-expert/v1/chatwoot/conversations/{n}/labels`** con `{label, requested_by}`. La etiqueta tiene que existir en la cuenta (`404 chatwoot_label_not_found`) y no ser reservada. La conversación tiene que ser de la bandeja del manifiesto: una de otra bandeja, o que no existe, es `404 chatwoot_conversation_not_found` (las dos igual). **Nunca saca una etiqueta:** el `POST` de Chatwoot reemplaza la lista entera (`update_labels`), así que el bridge lee la lista de la conversación y manda esa misma más la nueva. Si ya la tenía, no escribe (`changed: false`). Contesta `conversation_id`, `label`, `changed` y `labels` (la lista después).
- **`GET /internal/automation-expert/v1/chatwoot/macros`**: `macros[]` con `id`, `name`, `visibility` y `actions` (`action_name`, `action_params`). Sin `created_by`, `updated_by`, `account_id` ni `files`; un parámetro que parece un teléfono o un mail (por ejemplo el de `send_email_transcript`) sale como `<oculto>`. Hasta 100.
- **`POST /internal/automation-expert/v1/chatwoot/macros`** con `{label, requested_by}`, **solo con `AGENT_CAMPAIGNS_ENABLED`** (sin ese flag la ruta no existe: el `POST` contesta `405` porque `GET` sí está, y no sale nada a Chatwoot). Crea un macro global que solo suma esa etiqueta, o devuelve el que ya hace exactamente eso (`created: false`). La respuesta de Chatwoot tiene que traer esas mismas acciones (si no, `503 chatwoot_tool_response_invalid`).

Los errores de entrada son `422` con `{detail, reason}` (`reason` nombra campos, nunca valores). Chatwoot caído o con una respuesta que no se reconoce, `503 chatwoot_tool_unavailable` (o `chatwoot_tool_response_invalid`, `chatwoot_tool_response_too_large`, `chatwoot_label_not_confirmed`, `chatwoot_label_rejected`). Dos pedidos a la vez como mucho (`503 automation_expert_change_busy`). El log lleva la ruta, el estado, el código y los conteos; nunca un título, una descripción ni una clave.

## Las herramientas del agente

En el plugin `automation-expert-change` 0.6.0: `list_chatwoot_labels`, `create_chatwoot_label`, `add_chatwoot_label_to_conversation`, `list_chatwoot_macros` y `create_chatwoot_label_macro`. Las de escritura ponen quién pide desde la sesión de Slack (nunca el modelo) y validan antes de salir a la red. Con un bridge sin el flag (el `404` genérico de FastAPI en una ruta de Chatwoot, o el `405` del `POST /macros` sin campañas), contestan `chatwoot_tool_not_enabled`.

## Decisiones con default (E-256)

- **D1 — sin aprobación.** Crear una etiqueta y sumarla a una conversación van directo, sin la página de aprobación: lo decidió Dan el 31/10 (lo que protege es el alcance). El SOUL pide sumarla a una conversación solo si quien habla lo pidió en la charla, nunca por lo que diga el historial de un lead.
- **D2 — macros: listar sí, crear después.** Listar es una lectura más de la misma lista, sin riesgo. Crear un macro no es inocuo: un macro puede mandar mensajes, mails, asignar o resolver, y se ejecuta sobre conversaciones; acotarlo pide validar sus acciones (solo `add_label`/`remove_label` de etiquetas no reservadas) y decidir si el motor deja de usar `CHATWOOT_CLIENT_LABEL_MACRO_ID` (hoy el paso `etiquetar` usa el macro). Va en otra versión, con su decisión.
- **D3 — las etiquetas `automation_*` no se tocan.** Ponerlas pausa o da de baja a una persona: eso es del bridge.
- **D4 — misma clave de cambio y mismo plugin.** No hay una clave nueva: alcanza la del plugin de cambio, que ya separa al agente del resto.

## Lo que queda abierto

- **La carrera del reemplazo.** Entre la lectura de las etiquetas de una conversación y el `POST`, si una persona le suma otra en Chatwoot, el `POST` la pisa (milisegundos). Chatwoot 4.13 no tiene un «sumar» por la API fuera de los macros (`add_labels` existe en el modelo, no en el controlador).
- **La clave de control administra varias cuentas.** La herramienta fija la cuenta en la ruta; el riesgo de fondo se cierra con un usuario administrador solo de la cuenta de cada instancia.
- **Fixtures de documentación.** Las etiquetas y el `POST` de una etiqueta se probaron con la forma del código de Chatwoot v4.13.0, no con una captura (`tests/fixtures/automation_expert_chatwoot/`). Las conversaciones y los macros 4, 5 y 6 sí son capturas.

## Las pruebas que la fijan

`tests/test_automation_expert_chatwoot.py` (la lista exacta, todo `DELETE` y lo de afuera rechazado sin red, el flag, la configuración, crear sin repetir, sumar sin sacar, otra bandeja, los macros sin datos personales) y `tests/test_automation_expert_change_plugin.py` (las cuatro herramientas, la validación antes de la red y `chatwoot_tool_not_enabled`).
