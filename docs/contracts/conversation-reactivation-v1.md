# Contrato — reactivacion de conversaciones fuera de ventana V1

- **Estado:** construido; default-off; pendiente de activacion y E2E.
- **Objetivo:** producir el mensaje entrante que le falta al sistema de reanudacion, mandandole una plantilla aprobada de Meta al lead que quedo esperando fuera de la ventana de servicio de 24 h.

## Por que existe

El 2026-09-23 se desplego y activo el sistema de reanudacion
([`resume-paused-conversation-v1.md`](resume-paused-conversation-v1.md)): una
conversacion pausada vuelve a ser admisible cuando el equipo lleva horas sin
atenderla. Pero su disparador vive **dentro del worker del webhook de
Chatwoot**: corre unicamente cuando entra un mensaje del lead.

Si el lead ya escribio y esta esperando, no va a escribir de nuevo. A las 19:32
UTC de ese mismo dia, `conversation_resume_events` tenia **0 filas**: el sistema
recien activado no se habia disparado una sola vez, y las 6 conversaciones que
esperaban respuesta seguian exactamente igual.

Medido sobre el inbox 9 (WABA `whatsapp_cloud`, canal 8) a las 19:36 UTC, las
conversaciones abiertas cuyo ultimo mensaje publico era del lead:

| conv | `can_reply` | espera | etiquetas | nombre del contacto |
|---|---|---|---|---|
| 124 | `true` | 9,6 h | `automation_paused` | Patricia Garcia |
| 110 | `true` | 17,0 h | `automation_paused` | Andres felipe galvis loaiza |
| 143 | `false` | 24,5 h | `automation_paused` | Chayin |
| 136 | `false` | 25,9 h | `automation_paused` | Angel Crown |
| 63 | `false` | 27,2 h | `automation_paused` | Marcia Aidegart Narvaez Lara |
| 126 | `false` | 28,2 h | `automation_paused` | Mau |

El ciclo completo que este contrato cierra:

```
plantilla -> el lead responde -> entra el webhook -> resume_paused_conversation
levanta la pausa -> el agente retoma la conversacion
```

## La senal de ventana: `can_reply`, no un reloj propio

La ventana de servicio de Meta dura 24 h desde el ultimo mensaje del usuario.
Chatwoot ya la publica como `can_reply`, asi que el barredor **la lee, no la
calcula**: con `can_reply=true` todavia entra texto libre y la conversacion le
corresponde al [monitor de conversaciones
estancadas](chatwoot-stalled-conversation-monitor-v1.md); con `false`, solo
entra una plantilla aprobada.

`can_reply` ausente o no-`false` cuenta como dentro de la ventana. Un booleano
que no se pudo leer no autoriza mandar una plantilla de marketing.

## Criterio canonico

Un candidato solo se admite cuando Chatwoot confirma, en el momento del barrido:

1. cuenta e inbox exactos;
2. conversacion `open`, no silenciada y no pospuesta;
3. **`can_reply=false`** (ventana de servicio cerrada);
4. sin la etiqueta `automation_opted_out`;
5. contacto con `blocked` explicitamente `false` (ausente o nulo excluye);
6. telefono E.164 legible desde `meta.sender.phone_number` o
   `meta.sender.identifier`, y dentro del scope configurado
   (`ALLOWED_WHATSAPP_JID` o `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`);
7. nombre de contacto del que se pueda extraer un primer nombre usable;
8. el ultimo mensaje publico conversacional, ordenado por **ID entero de
   Chatwoot y no por `created_at`**, es un inbound (`message_type` 0) del
   contacto, con contenido no vacio. Las actividades de sistema
   (`message_type` 2) no participan; las notas privadas tampoco; un tipo publico
   desconocido o malformado invalida el historial entero en vez de ignorarse;
9. antiguedad de ese inbound entre `CONVERSATION_REACTIVATION_MIN_AGE_SECONDS`
   (86.400 por defecto) y `CONVERSATION_REACTIVATION_MAX_AGE_SECONDS`
   (2.592.000, 30 dias);
10. silencio del equipo mayor o igual a `CONVERSATION_RESUME_QUIET_SECONDS`
    (28.800, el mismo umbral que la reanudacion).

**La etiqueta `automation_paused` NO excluye.** Es exactamente el caso: las 6
conversaciones que esperaban respuesta el 23/09 estaban pausadas. Excluirlas
dejaria al barredor sin nadie a quien reactivar.

**El assignee tampoco es senal**, en ninguna direccion. Las derivaciones de este
inbox van al team 1 sin asignado individual, asi que la conversacion figura sin
humano incluso mientras una persona la esta contestando. Lo que se mide es el
silencio real, y si un mensaje del equipo tiene una fecha ilegible, la
conversacion se saltea (fail-closed) en vez de tratarse como silencio.

En el caso normal el silencio del equipo es redundante con el criterio 8: si el
equipo contesto despues, el ultimo conversacional ya no es del lead. Donde no es
redundante es cuando el orden por ID y las fechas no coinciden.

## La plantilla

Se lee del catalogo del inbox (`GET /inboxes/{id}`) **en cada barrido**, no de
una variable de entorno. Si Meta da de baja o pausa la plantilla, el catalogo lo
refleja y el envio se corta, en vez de producir mensajes rechazados contra una
plantilla que el bridge cree vigente.

Se exige: que exista con el nombre configurado
(`WABA_REACTIVATION_TEMPLATE_NAME`), `status=APPROVED`, un unico componente
`BODY`, y **exactamente un marcador, `{{1}}`**. Con cero marcadores el nombre no
entra a ningun lado; con dos, Meta rechaza el envio por falta de parametros y el
lead no recibe nada. Si `WABA_REACTIVATION_TEMPLATE_LANGUAGE` esta declarado, el
idioma tiene que coincidir.

El parametro `{{1}}` es **el primer nombre**, no el nombre completo. Chatwoot
guarda el push name de WhatsApp, que puede ser un nombre completo en minusculas,
un apodo, un telefono o vacio: de los seis nombres reales medidos, saludar con
el nombre completo queda mal en cuatro. Sin un primer nombre usable **no se
manda**: una variable vacia hace fallar el envio en Meta.

El mensaje sale como el **AgentBot**, no como un usuario de Chatwoot: un
saliente de un `user` es justamente lo que pausa la automatizacion, y reactivar
pausando seria contraproducente. Lleva `reactivation_command_key` en
`content_attributes` para poder cruzar, desde Chatwoot, que envio corresponde a
que fila de auditoria.

## Reserva, envio y cierre

El ciclo de vida de cada intento vive en `public.conversation_reactivation_events`
y va `claimed -> sent | failed`:

1. **`claim_conversation_reactivation`** reserva **antes** de llamar a Chatwoot.
   Devuelve `claimed`, `replayed`, `blocked_contact`,
   `blocked_reactivation_limit` o `not_found`.
2. El bridge manda la plantilla.
3. **`settle_conversation_reactivation`** cierra la fila como `sent` (con el id
   del mensaje) o `failed` (con el tipo de error).

Si el proceso muere entre la reserva y el envio, la fila queda en `claimed` y
**bloquea el reintento**. Es deliberado y es el lado seguro del error: mandarle
dos veces una plantilla de marketing al mismo lead es peor que no mandarsela.
`failed` es el unico estado que libera, porque ahi Chatwoot dijo explicitamente
que no salio.

- **Idempotencia:** `command_key = reactivate:<conversation_id>:<message_id>`,
  con indice unico **parcial** (`where status <> 'failed'`).
- **Limite:** `CONVERSATION_REACTIVATION_MAX` (1 por defecto) por conversacion,
  contando las filas `claimed` y `sent`. A quien recibio la plantilla y no
  contesto, no se le insiste.
- **Techo por barrido:** `CONVERSATION_REACTIVATION_MAX_SENDS_PER_SCAN` (10).
  Un error de criterio no le escribe al inbox entero de una.

La reactivacion **no levanta la pausa**. Eso lo hace `resume_paused_conversation`
cuando el lead contesta, con su propia auditoria. Si los dos caminos escribieran
`human_takeover`, ninguna auditoria seria completa. Una conversacion reactivada
que nadie contesta se queda exactamente como estaba.

Una conversacion que Chatwoot conoce y Supabase no (sin caso comercial) devuelve
`not_found` y no se manda nada. Medido el 23/09: cuatro conversaciones del inbox
9 estaban en ese estado.

## Activacion

`CONVERSATION_REACTIVATION_ENABLED=false` por defecto. Al activarlo, el bridge
**no arranca** si falta la plantilla declarada, los IDs canonicos de cuenta e
inbox, el AgentBot con su token, o un scope de remitente acotado. Mandar una
plantilla de marketing es un efecto externo irreversible: arrancar a medias no
es una opcion.

La configuracion numerica se valida **este el gate prendido o apagado**, para que
un valor invalido no se descubra recien el dia que alguien lo enciende.

| Variable | Default |
|---|---|
| `CONVERSATION_REACTIVATION_ENABLED` | `false` |
| `WABA_REACTIVATION_TEMPLATE_NAME` | (vacio) |
| `WABA_REACTIVATION_TEMPLATE_LANGUAGE` | (vacio, no se verifica) |
| `CONVERSATION_REACTIVATION_INTERVAL_SECONDS` | `900` |
| `CONVERSATION_REACTIVATION_MIN_AGE_SECONDS` | `86400` |
| `CONVERSATION_REACTIVATION_MAX_AGE_SECONDS` | `2592000` |
| `CONVERSATION_REACTIVATION_MAX` | `1` |
| `CONVERSATION_REACTIVATION_MAX_SENDS_PER_SCAN` | `10` |
| `CONVERSATION_REACTIVATION_MAX_PAGES` | `5` |

## Observabilidad

`/ready` publica `conversation_reactivation` con el estado del ultimo barrido
(`never`, `healthy`, `error`, `stopped`) **solo cuando el barredor esta
construido**: un despliegue que no lo usa conserva su payload de readiness sin
cambios.

Un barrido fallido **no responde 503**. El barredor no atiende a nadie en vivo, y
tumbar el bridge entero por eso dejaria de contestarle a los leads que si estan
escribiendo.

Los logs llevan solo el id de conversacion, el motivo del salteo y tipos de
error; nunca contenido ni identidad del contacto.

## El arreglo que vino con esto

La pausa se escribe por dos caminos independientes: la etiqueta
`automation_paused` la pone **cualquier** saliente de un `user` de Chatwoot, y
`human_takeover` lo pone **solo** una derivacion durable. Medido el 2026-09-23
sobre el inbox 9: de 27 conversaciones con la etiqueta, **8 no tenian
`human_takeover`** (114, 126 y 133 entre las abiertas).

El disparador de reanudacion estaba condicionado a que la admision devolviera
`blocked`, asi que para esas ocho nunca corria: la admision pasaba, el agente
razonaba, y recien la guarda de pre-envio lo frenaba por la etiqueta que nadie
sacaba. El disparador ahora corre siempre que el gate este activo, y la
re-admision queda condicionada a que efectivamente hubiera estado bloqueada.

## Lo que este contrato NO cubre

1. **Las conversaciones dentro de la ventana** (`can_reply=true`) que ademas
   estan pausadas. Hoy no las atiende nadie: el monitor de estancadas las excluye
   por la etiqueta. El barredor las alcanza igual cuando cruzan las 24 h, con esa
   demora. Resolverlo antes exige tocar el criterio canonico del monitor.
2. **Las 75 conversaciones cuyo ultimo mensaje es nuestro** y el lead no
   contesto. Ese es el caso `no_reply_review` del motor de seguimientos, otro
   problema y otro contrato.
3. **La validacion del `content` devuelto por Chatwoot.** `send_followup_message`
   compara el contenido que Chatwoot devuelve contra el que se mando;
   `send_reactivation_template` no lo hace, porque no hay dato medido de que
   Chatwoot conserve el texto sin normalizar cuando el mensaje lleva
   `template_params`. Se agrega cuando el E2E lo muestre.

## Corrida en seco contra produccion (2026-09-23 20:30 UTC)

`evaluate_reactivation_candidate` corrido con los payloads reales de las **25
conversaciones abiertas** del inbox 9, sin reservar ni enviar nada:

| resultado | conversaciones |
|---|---|
| **candidatas** | **4**: 143 (25,3 h), 136 (26,6 h), 63 (27,9 h), 126 (28,9 h) |
| `inside_service_window` | 4: 124, 110, 153, 152 |
| `last_message_not_inbound` | 15 |
| `contact_name_unusable` | 2: 123 y 133 |

Los nombres renderizados fueron `Chayin`, `Ángel`, `Marcia` y `Mau`, y los cuatro
mensajes empiezan `Hola, <nombre>. Soy el asistente virtual...`.

**Sobre los dos `contact_name_unusable`:** los push names son `✨` y `VM🌷`. Las
dos se habrian descartado igual por el criterio 8 (su ultimo mensaje es
nuestro), asi que la barrera del nombre **no perdio ninguna candidata real**.
Pero el caso existe: un contacto cuyo push name es solo un emoji queda afuera. Es
deliberado — `Hola, ✨.` es peor que no escribir — y se prefiere fail-closed
antes que inventar un saludo.

## Verificacion pendiente

Antes de dar el feature por terminado: aplicar la migracion, desplegar con el
gate apagado, comprobar `/ready`, activar, y confirmar en el primer barrido que
(a) aparece una fila `sent` en `conversation_reactivation_events`, (b) el lead
recibio el mensaje, (c) al responder, aparece una fila en
`conversation_resume_events` y el agente contesta, y (d) delta cero en las
conversaciones de control (las que estaban dentro de la ventana no reciben nada).
