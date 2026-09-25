# Contrato — reanudar una conversación pausada V1

- **Estado:** implementado; `CONVERSATION_RESUME_ENABLED=false` por defecto; pendiente de activación y E2E.
- **Fecha:** 2026-09-23
- **Objetivo:** que la pausa de una conversación deje de ser terminal, sin que el agente se meta donde una persona está atendiendo.

## Por qué existe

La pausa se escribía en dos capas y ninguna tenía vuelta:

1. **Chatwoot.** Cualquier mensaje de un `user` hace que `filtering.py` decida `pause_automation`, y la etiqueta `automation_paused` se aplica ejecutando el macro de pausa. La guarda pre-envío (`chatwoot.py`) bloquea el envío mientras esa etiqueta exista.
2. **Supabase.** Toda derivación deja la conversación en `paused_human` / `paused` / `human_takeover=true` y el caso en `paused` / `disabled`. La guarda de admisión devuelve `blocked` con eso, así que el agente no vuelve a correr.

Medido el 2026-09-23 sobre el inbox 9: **27 de 109** conversaciones pausadas; de las **10** donde el lead escribió último y esperaba respuesta, **9** estaban pausadas, y tres habían pedido el enlace de pago con la emisión en `reserved`.

## Qué señal se usa, y cuál no

**No sirve el assignee.** Las derivaciones van al team `johanna - revisión humana` pero **sin assignee individual**: el equipo trabaja desde la cola sin auto-asignarse. `human_assignee_present` es falso incluso mientras una persona está respondiendo, así que "no hay humano asignado" no distingue nada por sí solo.

La señal es el **silencio del equipo**: segundos desde el último mensaje público escrito por un `user` de Chatwoot. Las actividades de sistema (`message_type` 2) no cuentan — asignar una conversación no es atenderla — y las notas privadas tampoco. Si un mensaje del equipo existe pero su fecha no se puede leer, falla cerrado: tratar eso como silencio reactivaría una conversación que alguien podría estar atendiendo en ese momento.

Umbral por defecto: **28.800 s (8 h)**, configurable. Calibrado contra el caso real de la conversación 110, donde el equipo escribió a las 16:11 y el lead respondió 10 h 30 después.

## Cuándo se reactiva

Al entrar un mensaje del lead cuya admisión devolvió `blocked`, y sólo si **todo** esto se cumple:

- la conversación está `open` y `can_reply`;
- no tiene assignee humano;
- tiene la etiqueta `automation_paused` (si no la tiene, no hay nada que levantar);
- no tiene `automation_opted_out`;
- el silencio del equipo alcanza el umbral, **o** ninguna persona escribió nunca en esa conversación;
- el contacto no está `opted_out`, `blocked` ni `restricted`;
- no hay una derivación a medio proyectar (`requested` / `projection_failed`);
- no se alcanzó el límite de reactivaciones de esa conversación.

## Orden de las dos capas

Primero la capa durable (Supabase), después la etiqueta de Chatwoot. Si la etiqueta no se puede sacar, **no se reintenta la admisión**: la guarda pre-envío bloquearía el envío igual y el agente terminaría derivando, así que fabricar una respuesta que no sale sólo agrega ruido. La conversación vuelve al estado anterior en la próxima derivación.

## Idempotencia y límite

`command_key` es `resume:<conversation_id>:<message_id>` y tiene índice único: el mismo mensaje disparador no reactiva dos veces (`replayed`). Cada reactivación deja una fila en `conversation_resume_events` con el estado previo de las dos entidades, el motivo y el silencio medido.

El **límite anti-loop** (por defecto 3 por conversación) existe porque el ciclo natural es reactivar → el agente vuelve a derivar → pausa → reactivar. Pasado el límite, la conversación se queda con las personas y el sistema no insiste.

## Resultados de la RPC

`resumed` · `replayed` · `already_active` · `blocked_contact` · `blocked_pending_handoff` · `blocked_resume_limit` · `not_found`.

Sólo los tres primeros habilitan reintentar la admisión.

## Qué ve el agente al retomar

Mientras `CONVERSATION_RESUME_ENABLED` esté activo, el historial canónico incluye los mensajes escritos por personas del equipo con el actor **`human_agent`** (antes se descartaban, y el agente retomaba ciego a lo que el humano había dicho o prometido). El `SOUL.md` define qué hacer con ellos: leerlos como dicho, no repetirlos, no contradecirlos, y derivar con `commercial_exception` si comprometen algo que el agente no puede cumplir.

## Lo que este contrato no cubre

- **No despausa sola una conversación en la que nadie escribe.** El disparador es un mensaje entrante del lead; una conversación pausada que el lead abandonó se queda como está.
- **No quita el `team_id`**: la conversación sigue visible en la cola del equipo.
- **No reabre conversaciones resueltas.** Chatwoot las reabre solo al llegar un inbound, que es cuando corre esto.

## Lo que salió mal la primera vez (2026-09-23 23:11 UTC)

El sistema se activó el 2026-09-23 a las 19:16 UTC (`CONVERSATION_RESUME_ENABLED=true`, bridge en `fe1f5f3`, PR #174). No entró ningún mensaje del inbox 9 hasta las 23:11 UTC, cuando tres de los cuatro leads que habían recibido la plantilla de reactivación contestaron (PR #175, `a54b387`). **El bridge falló los cinco mensajes entrantes de esa hora** (conversaciones 126, 143, 158 y 63) con `chatwoot_work_failed error_type=UnboundLocalError`: 28 intentos, ninguna respuesta a ningún lead. Dan apagó el flag a las 23:52:45 UTC.

La causa: el disparador dentro de `process_chatwoot_work` pasaba `message_id=message_id` a `_resume_paused_conversation`, pero en el camino normal (sin mensaje de reset y sin planificación de descuento) ninguna rama anterior asignaba `message_id`. Como la misma función lo asigna más abajo, Python lo trata como local de toda la función y leerlo antes de asignarlo explota. En el PR #174 el call vivía dentro de la rama `blocked` de la admisión y el error quedó latente; el PR #175 lo sacó de esa rama para cubrir las ocho conversaciones etiquetadas sin `human_takeover`, y con eso pasó a correr en **cada** mensaje admitido. La conversación 158, un lead nuevo sin pausa, falló igual: el error estaba en el camino común, no en el de la pausa.

Por qué la suite estaba verde: ocho tests probaban `_resume_paused_conversation` aislada y uno más verificaba la **forma** del disparador sobre el AST. Ninguno ejecutaba `process_chatwoot_work` con `CONVERSATION_RESUME_ENABLED=true`. Un test sobre el AST comprueba que el código tenga la forma acordada; no comprueba que corra.

Dos cosas cambiaron a partir de eso:

1. El disparador toma `message_id` del payload y lo valida antes de llamar (entero positivo, como el resto de las identidades canónicas del bloque de admisión), y falla cerrado si no lo es.
2. Existe un test que ejecuta el handler completo con el webhook real del mensaje 2233 (`tests/fixtures/chatwoot_message_created_inbox_9_conv_158_20260923.json`) y los flags que definían el camino en producción, y exige que el worker no registre fallos **y** que el disparador haya llegado a consultar la conversación. Verificado que falla sobre el código anterior.

Además, `chatwoot_work_failed` ahora adjunta el traceback cuando el error no es un `RetryableChatwootWorkError`: 28 líneas idénticas sin la línea del error costaron horas de diagnóstico a ciegas.
