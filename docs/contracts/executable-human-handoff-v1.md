# Contrato V1 — handoff humano ejecutable

- **Estado:** Handoff V1 desplegado; guard de replay pausado implementado localmente y pendiente de publicación/migración/deploy
- **Versión:** 1
- **Fecha:** 2026-08-10
- **Alcance:** casos Lancemos con conversación Chatwoot canónica existente

## 1. Configuración

Todos los efectos permanecen apagados por defecto.

- `HUMAN_HANDOFF_PROJECTION_ENABLED=false`
- `HUMAN_HANDOFF_ADMISSION_ENABLED=false`
- `HUMAN_HANDOFF_PROJECTION_WORKER_ID`
- `HANDOFF_PROJECTION_POLICY_KEY`
- `HANDOFF_PROJECTION_POLICY_VERSION`
- `HANDOFF_NOTICE_ENABLED=false` (F-034, el aviso a la persona; ver abajo)
- `CUSTOMER_HANDOFF_NOTICE_ENABLED=false` (E-304, P-11 D12, el mismo aviso solo a clientas; ver abajo)
- `HANDOFF_LINK_NOTE_ENABLED=false` (F-034, D8, la nota privada con el link; ver abajo)
- `SAFETY_HANDOFF_REPLY_ENABLED=false` y `SAFETY_HANDOFF_HELP_LINE` (E-328, el mensaje de seguridad al derivar por riesgo para la persona; ver abajo)

Proyección habilitada exige Supabase, control plane Chatwoot, account/inbox canónicos y worker ID. Admisión habilitada exige además proyección, dispatcher outbound y perímetro Lancemos. Configuración incompleta impide arrancar.

### Aviso a la persona al derivar (F-034)

Decisiones D5 a D7 de Dan (24/10/2026). Con `HANDOFF_NOTICE_ENABLED=true` (exige
`HUMAN_HANDOFF_ADMISSION_ENABLED`), toda derivación del entrante le manda a la
persona un único mensaje fijo, del producto y no del modelo: «Le paso tu
consulta al equipo para que la revise.» Nombra al equipo, no promete plazo ni
que alguien vaya a responder, no da un nombre de persona y no dice que escribe
una IA. **Desde la 1.37.0 (E-486, D1)**: antes decía «…y te responden por este
mismo chat», que chocaba con el conocimiento de las aliadas. Si la derivación
cae fuera del horario del equipo, el aviso suma una segunda frase fija: «El
equipo atiende de lunes a viernes de 09:00 a 20:00 (hora de Ciudad de México).»
El horario sale de `[atencion]` del manifiesto (`dias`, `desde`, `hasta` en
`HH:MM`, `ciudad` opcional, en `instancia.zona_horaria`); sin el bloque rige el
de por defecto de las aliadas, 09:00 a 20:00 de lunes a viernes. Sin manifiesto
(Johanna), solo la primera frase.

```toml
[atencion]
dias = ["lunes", "martes", "miercoles", "jueves", "viernes"]
desde = "09:00"
hasta = "20:00"
```

- Sale antes de pedir la derivación durable y antes de la etiqueta
  `automation_paused`, por la misma guarda de envío que cualquier respuesta: si
  la conversación tiene una persona asignada, la pausa o un mensaje del equipo
  después del de la persona, no sale.
- Lleva `appointment_setter_decision = "handoff"` y el motivo en
  `appointment_setter_reason_code`. Esa marca es la que hace que salga una sola
  vez por conversación: si la conversación vuelve al agente y se deriva otra
  vez, no se repite.
- Un aviso que no sale nunca frena la derivación: queda en el log
  (`handoff_notice_skipped` / `handoff_notice_not_sent`) y la derivación sigue.
- No hay vuelta automática al agente (D7); la reanudación sigue como está.

### El mismo aviso, solo a clientas (E-304, P-11 D12)

Decisión D12 de Dan (02/11/2026). Con `CUSTOMER_HANDOFF_NOTICE_ENABLED=true`
(portable; exige `HUMAN_HANDOFF_ADMISSION_ENABLED` y
`AGENT_CUSTOMER_STATUS_ENABLED`), el aviso de arriba sale **solo** cuando la
entrada del agente de ese turno trae `customer_status` con algún producto
(`docs/contracts/customer-status-v1.md`): cualquier estado (`cliente`,
`reembolsada`, `contracargo`) y también la etiqueta `cliente`. Vale para toda
derivación del entrante de una clienta, también la de `customer_link_request`
(D6) y la forzada por salud. A quien no es clienta no le llega nada.

- Mismo texto, misma marca, mismo camino y mismas guardas que el aviso general:
  una vez por conversación, antes de la pausa, como respuesta al mensaje que la
  persona acaba de mandar (la ventana de 24 h de WhatsApp está abierta por
  construcción; ninguno de los dos avisos sale fuera de un turno entrante).
- Con `HANDOFF_NOTICE_ENABLED=true` este no agrega nada: el general ya avisa a
  todas y no se repite.
- Queda en el log revisado `customer_status_handoff_notice outcome=<sent|duplicate|already_sent|blocked|not_sent|not_configured> reason_code=<motivo> conversation=<n>`,
  sin teléfono ni mail. Un aviso que no sale nunca frena la derivación.
- Las derivaciones que no leen `customer_status` (el audio que no se pudo
  transcribir) no lo mandan.

### El mensaje de seguridad al derivar por riesgo (E-328)

Hasta E-328, al derivar el bridge descartaba el `reply` del agente (F-033),
también cuando la persona decía que se quería hacer daño: en el arnés de ATT1
(E-301 y E-322) el caso 75 derivó 2 de 3 veces, con un `reply` que le pedía
buscar ayuda de emergencia que nunca le llegaba.

El SOUL común de E-328 (un PR aparte, que se instala después de prender el flag) pide, ante riesgo para la salud o la vida (hacerse daño, no querer
vivir, dañar a otra persona, no estar a salvo), derivar con
`reason_code="safety_risk"` y poner en el `reply` la indicación de buscar
atención médica, ayuda de emergencia o una persona de confianza. Con
`SAFETY_HANDOFF_REPLY_ENABLED=true` (portable; exige
`HUMAN_HANDOFF_ADMISSION_ENABLED`), en esa derivación:

- el bridge le manda a la persona el `reply` del agente antes de pedir la
  derivación durable y antes de `automation_paused`, por la misma guarda de envío
  que el aviso de F-034, **en lugar** de ese aviso (uno solo);
- si el `reply` no sirve (vacío, más de 1000 caracteres o con una URL), manda el
  texto fijo del producto: «Lo que me cuentas es importante y mereces ayuda ahora
  mismo. Por favor, busca atención médica o llama a los servicios de emergencia
  de tu país, o pide a una persona de confianza que te acompañe.»;
- con `SAFETY_HANDOFF_HELP_LINE` (una línea, hasta 300 caracteres, sin link),
  la suma al final, separada por una línea en blanco: es donde va el número de
  la línea de ayuda del país de la instancia. El producto no trae números;
- lleva `appointment_setter_decision = "safety_handoff"` y
  `appointment_setter_reason_code = "safety_risk"`; no se limita a una vez por
  conversación;
- queda en el log revisado `safety_handoff_reply outcome=<sent|duplicate|blocked|not_sent|not_configured> conversation=<n>`,
  sin el texto ni datos de la persona. Un mensaje que no sale nunca frena la
  derivación;
- la tarjeta HND-001 de Slack sale como `[URGENTE] Riesgo para la persona:
  atender ya · <aliada>`, y los avisos de espera (E-309) nombran el motivo como
  «URGENTE: riesgo para la persona».

Con el flag apagado, o con cualquier otro motivo, la derivación sigue como hasta
hoy. El orden de activación importa: el flag se prende **antes** de cargar un SOUL
que pida `safety_risk`; con el SOUL nuevo y el flag apagado, esa derivación
callaría siempre.

### Nota privada con el link al derivar (F-034, D8)

Con `HANDOFF_LINK_NOTE_ENABLED=true` (exige `HUMAN_HANDOFF_ADMISSION_ENABLED`),
después de la derivación durable y de la etiqueta `automation_paused`, el bridge
escribe en la conversación una **nota privada** (nunca un mensaje a la persona)
con el último link de pago que le mandamos a esa persona.

- El link sale de la base, no de Chatwoot: `read_handoff_note_checkout_link_v1`
  (migración `20261025000300`, solo `service_role`) devuelve el último link
  `accepted_by_chatwoot` del contacto del caso en esa cuenta e inbox. Si es el
  del carrito (`recovery_first_touch`), trae el cupón vigente del caso y la nota
  muestra el link con su `offDiscount`, como el botón.
- Una nota por derivación: el marcador `[supportmagician-derivacion-link:<handoff_request_id>]`
  hace que un reintento del webhook no escriba otra.
- Si la persona nunca recibió un link nuestro, no hay nota y queda
  `handoff_link_note_skipped reason=no_link` en el log (decisión D-091-1, con
  default, pendiente de revisión de Dan).
- Una nota que no sale nunca frena la derivación: queda
  `handoff_link_note_not_sent` en el log y la derivación sigue igual.

Apagado (el valor por defecto), la derivación no le escribe nada a la persona.

## 2. Propuesta Hermes

El drafting durable acepta exactamente una de estas formas:

```json
{"strategy":"texto no vacío, máximo 120","message":"texto no vacío, máximo 500"}
```

```json
{"proposal":"suggest_handoff","reason_code":"commercial_exception"}
```

Los únicos `reason_code` válidos para handoff son:

- `explicit_human_request`;
- `commercial_exception`;
- `policy_requires_human`.

Esa taxonomía cerrada vive en `primary_reason_code` y no describe *por qué*
ocurrió esta derivación concreta. El motivo fino que compone el worker -- por
ejemplo `payment_link_purchase_already_approved`, cuando la emisión del enlace
devuelve `blocked` porque el contacto ya compró -- viaja aparte, en
`detail_reason_code` (`^[a-z][a-z0-9_]{0,99}$`, nullable). Un detalle mal
formado es un error de programación y la RPC lo rechaza; el bridge, en cambio,
prefiere pedir la derivación sin detalle antes que no pedirla: perder el motivo
es malo, perder la derivación deja a alguien esperando a nadie.

La sugerencia no inicia requests ni llama Chatwoot. El bridge invoca la RPC durable con action, attempt, worker y generación de lease.

## 3. Admisión durable

`request_human_handoff` recibe:

- `recovery_case_id`;
- `command_key` idempotente;
- motivo y requester;
- policy key/version;
- opcionalmente action/attempt/worker/generación, todos juntos.

`requested_by=agent` exige siempre esos cuatro campos y el fence de la reserva;
no existe un camino agent source-less. `system` y `operator` pueden originar un
stop operativo sin intento outbound.

La policy activa deriva scope piloto, equipo y nota. La RPC exige:

- caso no terminal con `conversation_id`;
- `pilot_recovery_case_bindings` coincidente;
- scope publicado Lancemos/WhatsApp/Hotmart/cart recovery;
- identidad seleccionada activa con account/inbox del scope;
- conversación del mismo contacto e identidad con ID externo positivo;
- source attempt todavía `reserved` y lease vigente cuando la sugerencia viene del dispatcher.

En una sola transacción:

1. crea o reutiliza un request;
2. fija snapshots inmutables de policy, scope y routing;
3. crea efectos `assignment` y `private_note`;
4. cierra reservas como `failed_before_request`;
5. conserva intentos `request_started` como `delivery_unknown` reconciliable sin
   vencimiento artificial; una aceptación tardía puede cerrar el intento pero no
   crear sucesores después del stop;
6. cancela acciones futuras;
7. pausa secuencia, caso y conversación.

Un replay con la misma command key y semántica responde `already_requested`, incluso si llega concurrentemente. La RPC serializa por command key antes de leer el replay. Cambiar motivo, requester, source o policy bajo la misma key produce `human_handoff_command_conflict`. Un request ya existente tampoco acepta evidencia bajo otra versión de policy.

### Replay de admisión inbound después del handoff

La existencia de una fila en `inbound_commercial_case_admissions` no concede
autoridad permanente para responder. `admit_inbound_commercial_case_v2` vuelve a
bloquear y releer, en la misma transacción:

- el caso exacto de la admisión;
- la conversación canónica del mismo contacto e identidad;
- scope y conversación externa de Chatwoot.

Sólo devuelve `created` o `already_exists` como replyable cuando el caso sigue
`active/draft_only`, la conversación sigue en un estado no terminal con
`automation_status=draft_only`, y `human_takeover=false`. Cualquier pausa,
deshabilitación o takeover devuelve:

```text
outcome=blocked
automation_status=disabled
```

El Bridge debe terminar ante `blocked` antes de reconstruir contexto o invocar
Hermes. La primera admisión no conserva autoridad durante el resto del work: el
Bridge vuelve a llamar al RPC V2 inmediatamente antes de leer o crear el
manifiesto multipart y dentro de la frontera del sender antes de cada parte. Un
`blocked`, `evidence_conflict` o error de reautorización produce cero efectos
nuevos; un error queda retryable. Esto cubre también propuestas y manifiestos ya
persistidos, que no pueden actuar como permiso de envío.

La reautorización durable precede al adapter externo y se complementa con las
dos lecturas canónicas finales de Chatwoot; no pretende convertir PostgreSQL y el
POST remoto en una transacción distribuida. Durante el rollout, el RPC legacy
`admit_inbound_commercial_case` traduce `blocked` a `evidence_conflict`, que las
versiones anteriores del Bridge ya bloquean. La función base queda sin `EXECUTE`
para roles API y `service_role`.

## 4. Proyección Chatwoot

Los efectos usan leases con owner, generación y vencimiento calculados contra el reloj de PostgreSQL, nunca contra el timestamp enviado por el caller. Claim deriva routing sólo del request snapshot y vuelve a comprobar caso, binding, scope, identidad y conversación canónica.

### Assignment

1. assignee con `assignee_type=User`: `applied`, sin POST;
   `AgentBot` no cuenta como humano y un tipo desconocido falla cerrado;
2. equipo esperado presente: `applied`, sin POST;
3. otro equipo sin persona: `conflict`, sin POST;
4. sin assignee ni equipo: POST del equipo esperado y GET de confirmación.

### Private note

La nota usa un marcador estable:

```text
[supportmagician-handoff:<request_id>:<template_key>:v<version>]
```

El worker escanea hasta un límite explícito que falla cerrado si no alcanza el borde del historial. Cero marcadores permite un POST; uno confirma idempotencia; más de uno es conflicto. El marcador es la clave de idempotencia de la nota, no decoración: sin él, cada reintento del efecto postearía una nota nueva.

Cuando el request trae `detail_reason_code`, el cuerpo de la nota lleva una
línea más, compuesta al crear el request y por lo tanto inmutable como el resto
del snapshot:

```text
Derivación inbound registrada por el bridge. La automatización quedó pausada y el caso requiere revisión humana.

Motivo: el contacto ya compró este producto, así que no se le envió un enlace de pago nuevo (payment_link_purchase_already_approved).

[supportmagician-handoff:<request_id>:<template_key>:v<version>]
```

`inbound_handoff_reason_sentence` traduce el código a esa frase. Un código que
todavía no esté en el mapa se muestra crudo en lugar de omitirse: un código feo
en la nota es mejor que una derivación sin motivo. Sin detalle, la nota queda
exactamente como antes.

El mapa cubre dos familias, y las dos importan:

- **Los que elige el agente** al pedir la derivación, que son los más
  frecuentes y los únicos válidos para `decision="handoff"` según su perfil:
  `explicit_human_request`, `commercial_exception` y `policy_requires_human`.
- **Los que compone el worker** cuando una regla convierte la propuesta en
  handoff: `direct_medication_guidance` y la familia `payment_link_*`, que lleva
  el `outcome` con el que la emisión del enlace devolvió `blocked`.

Cuando una regla del worker fuerza `handoff`, reescribe también el
`reason_code`: si sólo cambiara la decisión, la fila y la nota se quedarían con
el motivo que el agente había elegido para otra cosa. Tras un POST incierto, el estado `delivery_unknown` sólo permite escanear: nunca vuelve a crear la nota automáticamente y termina en `dead_letter` al alcanzar el límite de intentos.

La proyección durable no muta labels, macros ni mensajes públicos. En el flujo
inbound con respuesta automática, el work que originó el handoff aplica una
postcondición adicional y ordenada:

1. registra o reconcilia el handoff durable;
2. ejecuta el macro de pausa para asegurar `automation_paused`;
3. confirma la etiqueta y termina el work sin ejecutar ningún reply público.

La etiqueta ya presente es éxito idempotente. Un error HTTP/protocolo o una
postcondición no confirmada conserva el work en retry. El mensaje inbound que
dispara la derivación no recibe una respuesta automática: desde ese punto la
respuesta pertenece exclusivamente al equipo humano. Esta postcondición no
agrega un tercer efecto durable ni cambia que `assignment` y `private_note`
gobiernan el estado `projected` del request.

## 5. Estados

Request:

- `requested`;
- `projection_failed`;
- `projected`;
- `dead_letter`.

Efecto:

- `pending`;
- `retryable_failed`;
- `delivery_unknown`;
- `applied`;
- `conflict`;
- `dead_letter`.

Finalización exige lease vigente y generación exacta contra el reloj de PostgreSQL. Dos efectos `applied` llevan el request a `projected`. Un efecto en dead letter no reanuda automatización ni impide drenar su efecto hermano pendiente.

## 6. Readiness y rollback

Con proyección habilitada, `/ready` consulta `get_human_handoff_projection_status` y publica sólo conteos de pending, retryable, delivery unknown, conflicts y dead letters. Un fallo de la dependencia responde 503 `human_handoff_readiness_unavailable`.

Rollback seguro:

1. apagar admisión;
2. mantener proyección para drain;
3. resolver backlog/conflictos/dead letters;
4. apagar proyección.

Desactivar una policy impide requests nuevos y no altera snapshots ni efectos existentes.

## 7. Compatibilidad y evidencia

La migración y el runtime son aditivos y default-off. Las tablas niegan DML directo a roles API; sólo los RPC explícitos tienen `EXECUTE` para `service_role`. Policies, identidad de request, evidencia y tipo de efecto están protegidos contra mutación.

Las pruebas locales y PGlite del guard de replay no acreditan su migración remota
ni despliegue. La evidencia real del bypass y sus límites está en
`docs/operations/2026-08-26-inbound-paused-replay-e2e.md`.
