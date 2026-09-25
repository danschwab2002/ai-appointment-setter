# Contrato — reanudar una conversación pausada V1

- **Estado:** implementado; `CONVERSATION_RESUME_ENABLED=false` por defecto. Activado en producción el 2026-09-23 (PR #174, `fe1f5f3`); la primera reanudación real sigue pendiente (ver "Lo que salió mal la segunda vez").
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

Al entrar un mensaje del lead (haya devuelto `blocked` la admisión o no: la etiqueta sola también frena el envío), y sólo si **todo** esto se cumple:

- la conversación leída de Chatwoot es la del remitente del webhook: la identidad esperada es el JID que trajo `contact_inbox.source_id` (en modo scoped), **no** `ALLOWED_WHATSAPP_JID`. El show de la API no trae `contact_inbox` ni `meta.sender.identifier`, así que el cliente la confirma por `meta.sender.phone_number` en E.164 (ver "Lo que salió mal la segunda vez");
- la conversación está `open` y `can_reply`;
- no tiene assignee humano;
- tiene la etiqueta `automation_paused`; **o no la tiene pero la admisión devolvió `blocked`**. La etiqueta la saca el sistema al reanudar, nunca sola: su ausencia con la pausa durable vigente significa que una persona la sacó a mano desde Chatwoot para que el agente vuelva a contestar. Esa decisión se respeta: la pausa se levanta con `operator_request`, **sin medir el silencio del equipo** y sin ejecutar el macro (no hay etiqueta que sacar). Sin etiqueta y sin pausa durable no hay nada que levantar (ver "Lo que salió mal la tercera vez");
- no tiene `automation_opted_out`;
- el silencio del equipo alcanza el umbral, **o** ninguna persona escribió nunca en esa conversación (no aplica cuando una persona sacó la etiqueta);
- el contacto no está `opted_out`, `blocked` ni `restricted`;
- no hay una derivación a medio proyectar (`requested` / `projection_failed`);
- no se alcanzó el límite de reactivaciones de esa conversación.

## Orden de las dos capas

Primero la capa durable (Supabase), después la etiqueta de Chatwoot. Si la etiqueta no se puede sacar, **no se reintenta la admisión**: la guarda pre-envío bloquearía el envío igual y el agente terminaría derivando, así que fabricar una respuesta que no sale sólo agrega ruido. La conversación vuelve al estado anterior en la próxima derivación.

## Idempotencia y límite

`command_key` es `resume:<conversation_id>:<message_id>` y tiene índice único: el mismo mensaje disparador no reactiva dos veces (`replayed`). Cada reactivación deja una fila en `conversation_resume_events` con el estado previo de las dos entidades, el motivo (`inbound_after_quiet_period` cuando el sistema midió el silencio, `operator_request` cuando una persona sacó la etiqueta) y el silencio medido (nulo en el segundo caso).

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
- **No se entera cuando una persona pone o saca la etiqueta.** Chatwoot le manda al bridge solo `message_created` (la cuenta no suscribe `conversation_updated`, y las actividades como «X removed automation_paused» no son `webhook_sendable`). La decisión de la persona se aplica recién con el siguiente mensaje del lead: sacar la etiqueta habilita al agente desde ese mensaje; ponerla frena el envío desde el primer mensaje (la guarda pre-envío la lee), pero la reanudación por silencio del equipo la puede levantar igual pasadas las 8 h, porque hoy el sistema no distingue una etiqueta puesta por una persona de una puesta por él. Para cerrar ese hueco el ingreso ya **captura** los `conversation_updated` con cambio de `label_list` (ver `chatwoot-ingress-v1.md`); la sincronización se construye sobre ese payload real, cuando la cuenta suscriba el evento.

## Lo que salió mal la primera vez (2026-09-23 23:11 UTC)

El sistema se activó el 2026-09-23 a las 19:16 UTC (`CONVERSATION_RESUME_ENABLED=true`, bridge en `fe1f5f3`, PR #174). No entró ningún mensaje del inbox 9 hasta las 23:11 UTC, cuando tres de los cuatro leads que habían recibido la plantilla de reactivación contestaron (PR #175, `a54b387`). **El bridge falló los cinco mensajes entrantes de esa hora** (conversaciones 126, 143, 158 y 63) con `chatwoot_work_failed error_type=UnboundLocalError`: 28 intentos, ninguna respuesta a ningún lead. Dan apagó el flag a las 23:52:45 UTC.

La causa: el disparador dentro de `process_chatwoot_work` pasaba `message_id=message_id` a `_resume_paused_conversation`, pero en el camino normal (sin mensaje de reset y sin planificación de descuento) ninguna rama anterior asignaba `message_id`. Como la misma función lo asigna más abajo, Python lo trata como local de toda la función y leerlo antes de asignarlo explota. En el PR #174 el call vivía dentro de la rama `blocked` de la admisión y el error quedó latente; el PR #175 lo sacó de esa rama para cubrir las ocho conversaciones etiquetadas sin `human_takeover`, y con eso pasó a correr en **cada** mensaje admitido. La conversación 158, un lead nuevo sin pausa, falló igual: el error estaba en el camino común, no en el de la pausa.

Por qué la suite estaba verde: ocho tests probaban `_resume_paused_conversation` aislada y uno más verificaba la **forma** del disparador sobre el AST. Ninguno ejecutaba `process_chatwoot_work` con `CONVERSATION_RESUME_ENABLED=true`. Un test sobre el AST comprueba que el código tenga la forma acordada; no comprueba que corra.

Dos cosas cambiaron a partir de eso:

1. El disparador toma `message_id` del payload y lo valida antes de llamar (entero positivo, como el resto de las identidades canónicas del bloque de admisión), y falla cerrado si no lo es.
2. Existe un test que ejecuta el handler completo con el webhook real del mensaje 2233 (`tests/fixtures/chatwoot_message_created_inbox_9_conv_158_20260923.json`) y los flags que definían el camino en producción, y exige que el worker no registre fallos **y** que el disparador haya llegado a consultar la conversación. Verificado que falla sobre el código anterior.

Además, `chatwoot_work_failed` ahora adjunta el traceback cuando el error no es un `RetryableChatwootWorkError`: 28 líneas idénticas sin la línea del error costaron horas de diagnóstico a ciegas.

## Lo que salió mal la segunda vez (2026-09-25 20:41 UTC)

Con el `UnboundLocalError` corregido y el flag prendido de nuevo, el disparador corría en cada mensaje admitido y **nunca reactivó a nadie**: `conversation_resume_events` siguió en cero filas del 23/09 al 25/09. El caso que lo hizo visible fue la conversación 177: la plantilla de reactivación salió el 25/09 a las 20:36:59 UTC, el lead contestó a las 20:41:29, el worker marcó la entrega como `completed` a las 20:42:05 sin llamar a la RPC ni ejecutar el macro, y la conversación quedó `open`, `can_reply`, sin assignee, con la etiqueta `automation_paused` y sin que nadie la atendiera. Todas las condiciones de arriba se cumplían (silencio del equipo: 24 h).

La causa: `_resume_paused_conversation` leía la conversación con `get_canonical_conversation_snapshot` **sin `expected_jid`**. El cliente verifica la identidad de toda conversación que lee, y sin un JID esperado compara contra `ALLOWED_WHATSAPP_JID`, que en producción es el número de prueba. Como el show de la API no trae `contact_inbox` en la raíz y `meta.sender.identifier` llega `null` en WhatsApp Cloud, la verificación cae a `meta.sender.phone_number` y falla para todo lead real con `conversation_identity_mismatch`. La función atrapa ese error y devuelve `False` en silencio, y como el bridge no configura el nivel de logging, ni el `conversation_resume_skipped` ni ningún otro `info` llegan a la salida del contenedor: el fallo no dejó rastro en ningún lado. Es la misma clase de defecto que tuvo el monitor de estancadas el 24/09 (PR #178) y el barredor de reactivación en su primer despliegue: un criterio que descarta a todos termina igual que uno que no tiene a quién atender.

Por qué la suite estaba verde: los ocho tests de `_resume_paused_conversation` usaban un cliente falso que devolvía el snapshot armado a mano, y el test del handler completo (`test_the_resume_trigger_survives_a_real_inbound_with_production_flags`) construía el cliente real con `allowed_jid` igual al número del lead del fixture. Ninguno reproducía la condición de producción: remitentes acotados por scope y `ALLOWED_WHATSAPP_JID` distinto del lead.

Lo que cambió:

1. `_resume_paused_conversation` recibe `expected_jid` y se lo pasa a la lectura de la conversación y al retiro de la etiqueta; el disparador le pasa el JID del remitente del webhook (`scoped_expected_jid`, el mismo que usa el resto de la admisión). Con el JID del webhook, el teléfono E.164 del show lo confirma; con el JID de otro lead sigue fallando cerrado.
2. Fixture capturado `tests/fixtures/chatwoot_paused_lead_reply_inbox_9_conv_177_20260925.json`: el webhook del mensaje 2376 y el show + messages de la conversación 177, con la PII saneada.
3. Tres tests sobre ese fixture con el cliente real y `allowed_jid` = número de prueba: la función sin JID devuelve `False` sin llamar a la RPC (el comportamiento que tuvo producción), con el JID del webhook completa las dos capas (`resume:177:2376`, macro ejecutado, etiqueta fuera), y con el JID de otro lead falla cerrado; y el handler completo con los flags de producción reanuda a la 177 y vuelve a pedir la admisión. El del handler se corrió sobre el código anterior: rojo con el resultado exacto de producción (ninguna llamada a la RPC).

Lo que no cambia: el retiro de la etiqueta con `expected_jid` vuelve a leer la conversación para validar la autoridad antes de ejecutar el macro (una lectura más por reanudación). Y sigue pendiente configurar el nivel de logging del bridge, que hoy deja fuera todo `info`.

## Lo que salió mal la tercera vez (2026-09-25 18:18 UTC): la etiqueta quitada a mano

La conversación 173 estaba derivada desde el 24/09 18:21 UTC (`commercial_exception`: etiqueta `automation_paused`, equipo `johanna - revisión humana`, `paused_human` / `human_takeover=true` en Supabase). El 25/09 a las 18:18:30 el lead escribió (mensaje 2373; el webhook todavía traía la etiqueta) y a las 18:18:44 una persona del equipo **le sacó la etiqueta a mano** desde Chatwoot (actividad 2374) esperando que el agente volviera a contestar. El bridge cerró la entrega `completed` sin reanudar y Supabase siguió en `paused_human`: con el fix de la segunda vez desplegado, la conversación tampoco se habría reanudado, porque la función devolvía `False` si la etiqueta no estaba. Y no hay otro camino: el bridge solo recibe `message_created`, así que el cambio de etiqueta nunca le llega. Una conversación así quedaba pausada en Supabase para siempre, hasta que alguien le repusiera la etiqueta o le contestara. Ese día había 24 conversaciones con `human_takeover=true`.

Lo que cambió:

1. `_resume_paused_conversation` recibe `durable_pause_recorded` (la admisión devolvió `blocked`). Sin etiqueta y con la pausa durable vigente, la ausencia se lee como decisión de una persona: RPC con `operator_request`, `quiet_seconds` nulo, sin leer los mensajes ni ejecutar el macro. Sin etiqueta y sin pausa durable, sigue devolviendo `False`. La RPC sigue mandando: `blocked_pending_handoff`, `blocked_resume_limit` y `blocked_contact` dejan la conversación con las personas.
2. Fixture capturado `tests/fixtures/chatwoot_paused_lead_label_removed_by_human_inbox_9_conv_173_20260925.json`: el webhook del mensaje 2373 (con la etiqueta) y el show + messages leídos después (sin la etiqueta, con la actividad de la persona que la sacó), PII saneada.
3. Tests con el cliente real y `allowed_jid` = número de prueba: la función sin pausa durable no llama a la RPC; con pausa durable llama con `operator_request` sin medir el silencio y sin macro; y el handler completo con los flags de producción reanuda a la 173 y vuelve a pedir la admisión. El del handler se corrió sobre el código anterior: rojo (ninguna llamada a la RPC).
4. El ingreso captura los `conversation_updated` con cambio de `label_list` del inbox configurado (`202 captured`, sin trabajo durable), para construir la sincronización inmediata sobre un payload real.

Lo que no cambia: la conversación sigue esperando el siguiente mensaje del lead para reanudarse; el mensaje que ya llegó antes de que la persona sacara la etiqueta no se reprocesa.
