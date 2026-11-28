# Contrato — reanudar una conversación pausada V1

- **Estado:** implementado; `CONVERSATION_RESUME_ENABLED=false` por defecto. Activado en producción el 2026-09-23 (PR #174, `fe1f5f3`); la primera reanudación real sigue pendiente (ver "Lo que salió mal la segunda vez").
- **Fecha:** 2026-09-23
- **Versión:** 1.3 (2026-11-28). Suma la parte 3 de E-539 como propuesta detrás de `CHATWOOT_RESOLVED_CLOSE_ENABLED` (apagado): `close_resolved_conversation_v1` (migración `20261128300000`) cierra la conversación resuelta en una bandeja sin lock y libera al contacto, ver "Cierre al resolver". La 1.2 sumó la parte 1 de E-539 (E-577): `resume_paused_conversation_by_operator_v1` (migración `20261128200000`), la reanudación por la decisión **vista** de una persona, que marca atendida la derivación `projected` sin contestar y devuelve `not_paused` si la conversación no estaba pausada. La usa solo el camino del `conversation_updated` que saca `automation_paused`, detrás de `CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED` (apagado por defecto). Ver "Reanudación al sacar la etiqueta" e "Historial".
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

Hay dos disparadores. El de esta sección es el **mensaje entrante del lead**. El segundo, el `conversation_updated` que saca la etiqueta, es una propuesta detrás de flag y tiene sus propias reglas en "Reanudación al sacar la etiqueta".

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

`command_key` es `resume:<conversation_id>:<message_id>` y tiene índice único: el mismo mensaje disparador no reactiva dos veces (`replayed`). En el disparador por etiqueta sacada (propuesto) no hay mensaje que la ancle, y la clave sería `resume:<conversation_id>:label:<los primeros 32 hex del sha256 de la entrega de Chatwoot>` (cumple el formato `^[a-z0-9:_-]{1,200}$` de la RPC): un reintento de la misma entrega da `replayed`, y una segunda entrega de la misma remoción da `already_active`. Cada reactivación deja una fila en `conversation_resume_events` con el estado previo de las dos entidades, el motivo (`inbound_after_quiet_period` cuando el sistema midió el silencio, `operator_request` cuando una persona sacó la etiqueta) y el silencio medido (nulo en el segundo caso).

El **límite anti-loop** (por defecto 3 por conversación) existe porque el ciclo natural es reactivar → el agente vuelve a derivar → pausa → reactivar. Pasado el límite, la conversación se queda con las personas y el sistema no insiste.

## Resultados de la RPC

`resumed` · `replayed` · `already_active` · `blocked_contact` · `blocked_pending_handoff` · `blocked_resume_limit` · `not_found`.

⚠ **`blocked_pending_handoff` cambio de alcance el 2026-09-27** (migracion `20260927000300`). Antes cubria solo las derivaciones en `requested` y `projection_failed`, o sea los segundos en que el proyector de notas esta trabajando. Ahora incluye tambien las `projected` cuyo `attended_at` sigue nulo: la nota llego a Chatwoot y ninguna persona contesto todavia.

El efecto es que **la respuesta del lead ya no levanta la pausa** mientras el equipo no lo haya atendido. Antes si: la conversacion 186 se despauso dos veces (26/09 17:08 y 27/09 18:11) sobre una derivacion por pedido explicito de humano que nadie habia contestado, y el agente retomaba a quien habia pedido no hablar con el agente. Ver el [contrato de la atencion](handoff-attendance-v1.md) y el [ADR-0019](../decisions/0019-handoff-attendance-gates-automation.md).

Sólo los tres primeros habilitan reintentar la admisión.

### La RPC de la decisión vista (`resume_paused_conversation_by_operator_v1`, migración `20261128200000`)

La llama solo el camino de la etiqueta sacada ("Reanudación al sacar la etiqueta"), con `p_external_conversation_id`, `p_command_key` (la clave por entrega) y `p_max_resumes`. Es solo de `service_role`.

- **Solo una conversación pausada:** `human_takeover` o `paused_human`, y fuera de `completed`, `closed` y `blocked`. Si no, devuelve **`not_paused`** sin escribir nada. Es también lo que contesta al eco de la remoción que hace el propio bridge al reanudar.
- **Pausada:** marca atendidas (`attended_at = now`, nunca antes de su `created_at`) las derivaciones `projected` sin atender de los casos de la conversación, y delega en `resume_paused_conversation` con `operator_request`. La traza de quién atendió es esa fila de `conversation_resume_events`, escrita en la misma transacción.
- **`requested` y `projection_failed` siguen bloqueando** (`blocked_pending_handoff`, sin marcar nada): el proyector de notas está trabajando.
- **Si la vigente no reanuda** (`blocked_resume_limit`, `blocked_contact`, …), la marca se deshace y la salida es la de la vigente. El tope anti-loop también cuenta las decisiones de una persona.
- Con la clave ya usada da `replayed`; sin conversación, `not_found`. Una clave fuera de `^[a-z0-9:_-]{1,200}$` da `22023`.

Salidas: `resumed` · `replayed` · `already_active` · `not_paused` · `blocked_contact` · `blocked_pending_handoff` · `blocked_resume_limit` · `not_found`.

**Por qué es una RPC aparte y la vigente no cambia.** El camino del mensaje entrante deduce `operator_request` por la **ausencia** de la etiqueta. Pero la etiqueta y la pausa durable se escriben por caminos distintos: la etiqueta la pone el bridge después de pedir la derivación, y la nota la proyecta otro worker. Una etiqueta que nunca se llegó a poner se ve igual que una que sacó una persona. Si la vigente liberara la derivación sin atender con `operator_request`, devolvería al agente derivaciones que nadie atendió, como pasó con la conversación 186. Por eso la liberación vive solo en la vía de la decisión vista, el `conversation_updated`.

## Qué ve el agente al retomar

Mientras `CONVERSATION_RESUME_ENABLED` esté activo, el historial canónico incluye los mensajes escritos por personas del equipo con el actor **`human_agent`** (antes se descartaban, y el agente retomaba ciego a lo que el humano había dicho o prometido). El `SOUL.md` define qué hacer con ellos: leerlos como dicho, no repetirlos, no contradecirlos, y derivar con `commercial_exception` si comprometen algo que el agente no puede cumplir.

## Lo que este contrato no cubre

- **No despausa sola una conversación en la que nadie escribe.** El disparador es un mensaje entrante del lead; una conversación pausada que el lead abandonó se queda como está.
- **No quita el `team_id`**: la conversación sigue visible en la cola del equipo.
- **No reabre conversaciones resueltas.** Chatwoot las reabre solo al llegar un inbound, que es cuando corre esto.
- **Con `CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED` apagado (el default), no se entera cuando una persona pone o saca la etiqueta.** Los webhooks de las cuentas 1, 2 y 3 suscriben `message_created` y `conversation_updated` (relevado en E-539, 2026-11-13), pero el bridge solo **captura** los `conversation_updated` con cambio de `label_list` (ver `chatwoot-ingress-v1.md`) y no los procesa; las actividades como «X removed automation_paused» tampoco son `webhook_sendable`. Con el flag apagado, la decisión de la persona se aplica recién con el siguiente mensaje del lead: sacar la etiqueta habilita al agente desde ese mensaje; ponerla frena el envío desde el primer mensaje (la guarda pre-envío la lee), pero la reanudación por silencio del equipo la puede levantar igual pasadas las 8 h, porque el sistema no distingue una etiqueta puesta por una persona de una puesta por él. La sacada de la etiqueta la cubre la propuesta de "Reanudación al sacar la etiqueta"; la etiqueta **puesta** por una persona sigue sin sincronizarse en ningún caso.
- **Con `CHATWOOT_RESOLVED_CLOSE_ENABLED` apagado (el default), no cierra la conversación del setter cuando Chatwoot la resuelve.** Prendido, ver "Cierre al resolver".

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

Lo que no cambia: la conversación sigue esperando el siguiente mensaje del lead para reanudarse; el mensaje que ya llegó antes de que la persona sacara la etiqueta no se reprocesa. La propuesta de "Reanudación al sacar la etiqueta" (E-571) cierra esa espera cuando su flag esté prendido.

## Reanudación al sacar la etiqueta (propuesta, detrás de flag)

- **Estado:** propuesta, construida en E-571 (PR #468, rama `feat/claude-e571-derivaciones-chatwoot`, sin mergear al 2026-11-15). Es la parte 2 del arreglo que propuso el diagnóstico E-539. Este documento no afirma que esté desplegada ni prendida en ninguna instancia.
- **Flag:** `CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED`, `false` por defecto, en `PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES` y en `.env.example`. Apagado, el `conversation_updated` sigue el camino de hoy: se captura y no se procesa.

### Qué cambia con el flag prendido

Cuando una persona saca `automation_paused` de una conversación, a mano o con el macro de reanudación, el bridge reanudaría en el momento, sin esperar el próximo mensaje del lead:

1. **Ingreso.** `classify_chatwoot_label_removal` (`src/bridge/filtering.py`) reconoce el `conversation_updated` cuyo `changed_attributes` saca `automation_paused` de `label_list`, del inbox y la cuenta configurados (la cuenta se chequea si viene en el payload) y con el remitente acotado como en el resto del ingreso. Ese evento se captura igual que hoy y además entra al trabajo durable con la entrega (`X-Chatwoot-Delivery`) como clave: `202 accepted`, `reason: conversation_label_removal`; una entrega repetida da `200 duplicate`. Cualquier otro cambio de etiqueta, y todos con el flag apagado, solo se capturan.
2. **Worker.** `process_chatwoot_work` llama a `_resume_paused_conversation` con `durable_pause_recorded=True` y la clave por entrega de "Idempotencia y límite", en el modo `label_removal_only`, que llama a `resume_paused_conversation_by_operator_v1` (ver "La RPC de la decisión vista"; el motivo, `operator_request`, lo pone la base al delegar):
   - relee la conversación y **solo reanuda si la etiqueta sigue afuera**: si volvió, no hay decisión de una persona que respetar y no hace nada;
   - **nunca mide el silencio del equipo**: ese camino lo dispara solo un mensaje del lead;
   - no ejecuta el macro (no hay etiqueta que sacar) ni pide los mensajes de la conversación.
3. **Reintentos.** En este modo, una falla de red con Chatwoot o una falla de Supabase no se traga: levanta `LabelRemovalResumeTransientError` y el worker reintenta la entrega, **con un tope de 8 intentos**. La clave por entrega hace seguro el reintento. Un error de protocolo de Chatwoot (identidad que no coincide, por ejemplo) sigue fallando cerrado, sin reintento.

### Qué no cambia

- **Las reglas de la reanudación son las mismas de "Cuándo se reactiva"**, salvo las del silencio del equipo: la conversación `open` y `can_reply`, sin assignee humano, sin `automation_opted_out`, el contacto sin baja, sin derivación a medio proyectar y dentro del límite de reactivaciones.
- **Exige además** `CONVERSATION_RESUME_ENABLED` y la admisión durable (cut B, `CHATWOOT_CUT_B_ADMISSION_ENABLED`), el cliente de control de Chatwoot y Supabase. Si falta alguno, el flag queda inerte: no falla el arranque y el evento solo se captura.
- **La base decide.** El bridge no lee la pausa durable antes de llamar: la RPC de la decisión vista devuelve `not_paused` sin escribir si la conversación no estaba pausada y, si lo estaba, marca atendida la derivación `projected` sin contestar y reanuda (parte 1 de E-539). `requested` y `projection_failed` siguen dando `blocked_pending_handoff`.
- **Lo que no reanuda queda a la vista.** Cualquier salida que no reanuda, salvo `not_paused`, deja `chatwoot_label_removal_not_resumed conversation=… outcome=…` en `warning`, sin reintento: el tope, un contacto con baja o el proyector trabajando.
- El eco del propio bridge (la remoción que hace el sistema al reanudar) cuesta un snapshot y una RPC que da `not_paused`, en `info`. No hay loop.

### Antes de prender el flag

- Los tests de E-571 (`tests/test_chatwoot_label_removal_resume.py`) derivan el evento del webhook capturado de la conversación 173 más un `changed_attributes` con la forma del emisor (Chatwoot v4.13.0). El payload real de la entrega `7afec902-8c33-4239-b0a1-875ad3ada59d` (Maribel, conversación 260, 2026-11-12) está en la captura del bridge y no entró al repo. **Antes de prender el flag hay que sumarlo anonimizado como fixture con su test:** si `label_list` llega con otra forma, el clasificador falla cerrado y el flag no hace nada.
  - La forma que se espera de esa entrega: un `conversation_updated` de la cuenta 3, bandeja 13 y conversación 260. En la raíz trae el `webhook_data` de la conversación (`id`, `inbox_id`, `labels: []`, `contact_inbox.source_id`, `meta.sender`) y `changed_attributes: [{"label_list": {"previous_value": ["automation_paused"], "current_value": []}}]`. Esa conversación llegó además resuelta: el cambio de estado entró un segundo antes, a las 23:57:04Z, como `unsupported_event`.
  - Al anonimizarla: reemplazar nombre, teléfono, mail, `identifier`, avatar, `pubsub_token`, `source_id` e ids de contacto, y conservar ids de conversación, cuenta y bandeja, `labels`, `changed_attributes`, `status`, `can_reply`, horas y equipo. Documentarlo en un `_capture` con fecha y origen.
  - El test sobre el fixture tiene que probar dos cosas: que el clasificador la reconoce y que, por estar resuelta, no se llama a ninguna RPC.
- **La migración `20261128200000` tiene que estar aplicada, y PostgREST recargado, antes de subir el bridge que la llama.** Sin ella, la RPC da `404` (`PGRST202`) y la entrega se reintenta 8 veces y falla. Con el flag apagado no se llama.

## Pendiente: necesita migración (parte 3 de E-539)

La parte 1 la cubre `resume_paused_conversation_by_operator_v1` (E-577, ver "La RPC de la decisión vista"). La parte 3 no está construida; se describe acá como propuesta, para que el contrato diga qué falta.

- **Parte 1 (cubierta en 1.2).** Se construyó como una RPC aparte, y no con el parámetro `p_require_paused` sobre la vigente que proponía la 1.1. Una firma nueva habría dejado dos sobrecargas que PostgREST no sabe elegir y habría roto las huellas del inventario de la vigente. No se agregó la columna `attended_via`: la traza es la fila `operator_request` de `conversation_resume_events`.
- **Parte 3 (propuesta en 1.3).** Ver "Cierre al resolver".

## Cierre al resolver (propuesta, detrás de flag)

- **Estado:** propuesta, parte 3 de E-539, con las decisiones de Dan del 2026-11-28 (P-1 = cerrar solo en bandejas sin lock; liberar al contacto). Este documento no afirma que esté desplegada ni prendida en ninguna instancia.
- **Flag:** `CHATWOOT_RESOLVED_CLOSE_ENABLED`, `false` por defecto, en `PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES` y en `.env.example`. Es propio y no `CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED`: cerrar y reanudar son decisiones distintas, con su propia vuelta atrás. Apagado, el `conversation_updated` que cambia el estado sigue el camino de antes: `200 ignored` con `unsupported_event`, sin captura, sin trabajo durable y sin llamar a Chatwoot ni a la base (lo fija `tests/test_chatwoot_resolved_close.py`).

### Por qué existe

Las bandejas 11 y 13 tienen `lock_to_single_conversation = false`: cuando la persona vuelve a escribir, Chatwoot abre otra conversación y la vieja queda `paused_human` con `human_takeover` para siempre (25 de 50 el 2026-11-13, según E-539). Como las lecturas por contacto miran todas sus conversaciones, ese contacto queda «derivado» para siempre: sin primer contacto, sin disparadores nativos por audiencia ni por palabra clave y sin agente de recupero.

### Qué cambia con el flag prendido

1. **Ingreso.** `classify_chatwoot_conversation_resolved` (`src/bridge/filtering.py`) reconoce el `conversation_updated` cuyo `changed_attributes` trae `status` de otro valor a `resolved`, con el `status` de la raíz `resolved` o ausente, del inbox y la cuenta configurados y con el remitente acotado como en el resto del ingreso (la identidad sale del mismo helper que la etiqueta sacada). Ese evento se captura en `CAPTURE_DIR` y entra al trabajo durable con la entrega como clave: `202 accepted`, `reason: conversation_resolved`; una entrega repetida da `200 duplicate`. Reabrir, posponer y cualquier otra forma se ignoran como antes; una forma rota falla cerrado.
2. **Worker.** Relee la conversación con la identidad del remitente y solo sigue si sigue `resolved` (si no, `chatwoot_resolved_close_skipped … reason=status_open`). Después lee la bandeja (`GET /inboxes/<id>`) y **solo sigue si `lock_to_single_conversation` es exactamente `false`**: con lock, sin la clave o con otro valor no cierra (`reason=inbox_lock_<valor>`, `info`, sin reintento). Llama a `close_resolved_conversation_v1` con la clave `close:<conversación>:resolved:<los primeros 32 hex del sha256 de la entrega>`.
3. **Reintentos.** Una falla de red con Chatwoot (la conversación o la bandeja), una de la base o `blocked_pending_projection` levantan `LabelRemovalResumeTransientError` y el worker reintenta con el tope de 8 (unos 3 minutos). Un proyector en `projection_failed` por más tiempo deja la entrega `failed` y la conversación como estaba. Un error de protocolo de Chatwoot (identidad que no coincide) no se reintenta.
4. **Un mismo evento que resuelve y saca la etiqueta** entra por el camino de la etiqueta (si su flag está prendido): la reanudación no llega a la base porque la conversación no está `open`, y el cierre sí corre.

### La RPC (`close_resolved_conversation_v1`, migración `20261128300000`)

`(p_external_conversation_id bigint, p_command_key text, p_now timestamptz)`, solo `service_role`.

- **Solo una conversación que tomó el equipo** (`human_takeover`), fuera de `completed`, `closed` y `blocked`. Si no, `not_paused` sin escribir.
- **La cierra y libera al contacto:** `status = 'completed'`, `closed_at`, `human_takeover = false`, `automation_status = 'draft_only'`, `version + 1`; marca atendidas (`attended_at = now`, nunca antes de su `created_at`) las derivaciones `projected` sin atender de sus casos; deja el evento `chatwoot_resolved_conversation_closed` (`actor_type = human_agent`) con la clave, el estado anterior y cuántas derivaciones marcó.
- **No toca el caso comercial**: su guarda solo deja pausar y reanudar, y ninguna lectura por contacto mira el caso inbound.
- `requested` / `projection_failed` dan `blocked_pending_projection` sin escribir; `dead_letter` no frena. La clave ya usada da `replayed`; otra entrega sobre una ya cerrada por este camino, `already_closed`; sin conversación, `not_found`; una clave fuera de `^[a-z0-9:_-]{1,200}$`, `22023`.

Salidas: `closed` · `replayed` · `already_closed` · `not_paused` · `not_found` · `blocked_pending_projection`.

### Qué significa «liberar», lectura por lectura

Las lecturas vigentes que deciden si a un contacto se le escribe miran **todas** sus conversaciones:

| Lectura | Frena con | Con la conversación liberada |
|---|---|---|
| Primer contacto (`_portable_precheckout_stop_reason`) | `human_takeover`, `status` en `paused_human`/`closed`/`blocked`, `automation_status` en `paused`/`disabled`/`restricted`/`error` | pasa. Por eso el estado es `completed` y no `closed`: con `closed` seguiría frenado |
| Disparadores nativos por audiencia y palabra clave (`conversacion_derivada`) | lo mismo sin `closed` | pasa |
| Agente de recupero (`resolution.py`, `any_conversation_human_takeover`) | `human_takeover` | pasa |
| AE (`held`, `pending_handoff`) | `human_takeover`, `paused_human`, `automation_status` frenado, una derivación sin atender | deja de mostrarlo tomado |
| Avisos de derivación sin atender | la derivación con `attended_at` nulo | deja de avisar |

Lo prueban, llamando a las funciones vigentes, `validate_close_resolved_conversation.mjs` (el primer contacto y la palabra clave frenan antes y dejan pasar después; con `closed` el primer contacto sigue frenado). **Otra conversación del mismo contacto no se toca.** El agente de recupero puede volver a escribirle al contacto, y `close_recovery_case_without_pending_touch` deja de ver la conversación como tomada: es lo que se busca al liberar.

**En la misma conversación de Chatwoot** la admisión base no acepta una `completed`: da `evidence_conflict` (con un conflicto `canonical_drift`), donde una `paused_human` daba `blocked`, y ese mensaje quedaría sin respuesta. Por eso el worker solo cierra en bandejas sin lock (Dan, 28/11). **Deuda (b):** si una persona reabre a mano una conversación cerrada en una bandeja sin lock, pasa lo mismo; reabrirla en el setter con el `conversation_updated` de `resolved` a `open` queda para otro PR. Dentro de la misma deuda: la reanudación vigente por mensaje entrante no mira el estado y revive una `completed` a `active` (ese mensaje igual queda sin respuesta, porque la admisión dio `evidence_conflict`), y `claim_conversation_reactivation` tampoco mira el estado, así que en una conversación reabierta a mano, con la derivación ya atendida, la reactivación puede mandar su plantilla. La decisión vista (la etiqueta sacada) da `not_paused` sobre una cerrada.

**Deuda (carrito):** una derivación de un caso de recupero de carrito pausa el `recovery_cases` (`20260810000400`), y el cierre no lo toca. Para ese producto el primer contacto sigue frenado (`superseded_by_provider_event`) y el agente de recupero lo ve abierto. Liberar cubre la derivación del entrante (`inbound_sales`); qué hacer con un caso de recupero pausado de una conversación resuelta queda para otro PR (cambia el motor de seguimientos).

### Antes de prender el flag

- Un `conversation_updated` real de un cambio a `resolved` y un `GET` real de la bandeja, anonimizados, como fixtures con su test. Los tests derivan el primero del `webhook_data` de la conversación 173 más un `changed_attributes` con `status` en la forma del emisor (Chatwoot v4.13.0), y la bandeja de la fila capturada de la base (`inbox_binding_version/chatwoot_inboxes_20261025.json`, que tiene la columna). Con el flag prendido, la primera entrega real queda capturada antes de procesarse; si el `GET` de la bandeja no trae la clave, no se cierra nada.
- La migración `20261128300000` aplicada y PostgREST recargado antes de subir el bridge que la llama.

## Historial

- **1.0 (2026-09-23).** El contrato, con las correcciones de la primera, segunda y tercera vez (2026-09-23 al 2026-09-27) y el cambio de alcance de `blocked_pending_handoff` (`20260927000300`).
- **1.3 (2026-11-28, parte 3 de E-539).** El cierre al resolver como propuesta detrás de `CHATWOOT_RESOLVED_CLOSE_ENABLED`, con las decisiones de Dan del mismo día: solo en bandejas sin `lock_to_single_conversation`, y liberando al contacto (`completed`, sin `human_takeover`, `draft_only`, la derivación `projected` atendida). Deuda: reabrir al pasar de `resolved` a `open`, y los fixtures reales. No afirma nada desplegado.
- **1.2 (2026-11-28, E-577).** La parte 1 de E-539: `resume_paused_conversation_by_operator_v1` (migración `20261128200000`), solo para el camino de la etiqueta sacada. Marca atendida la derivación `projected` sin contestar, devuelve `not_paused` si la conversación no estaba pausada y no cambia la vigente. El cliente de `resume_paused_conversation` acepta `blocked_resume_limit`, que antes rechazaba como salida inválida. Anota la forma esperada del fixture de la conversación 260. No afirma nada desplegado.
- **1.1 (2026-11-15, E-578).** Suma el disparador por `conversation_updated` que saca `automation_paused` (E-571, PR #468), como propuesta detrás de `CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED`: clave por entrega, modo `label_removal_only`, reintentos con tope de 8. Corrige "Lo que este contrato no cubre": los webhooks ya suscriben `conversation_updated` (E-539). Anota las partes 1 y 3 de E-539 como pendientes con migración (E-577). Escrito antes del merge de #468: no afirma nada desplegado.
