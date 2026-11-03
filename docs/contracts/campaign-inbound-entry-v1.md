# La entrada a una campaña por el primer mensaje

- **Estado:** escrito al construir E-317, el 2026-11-03, antes de cualquier merge o despliegue. Describe la interfaz; no dice que nada esté desplegado ni activado. Todo va detrás de `CAMPAIGN_INBOUND_ENTRY_ENABLED`, apagado por defecto.
- **Contrato:** `campaign-inbound-entry-v1`, versión `1.0.0`.
- **Diseño:** `documentacion/2026-11-03-e319-diseno-campanas.md` (E-319, fuera del repo), §1.1, §1.2 y las decisiones D4, D5, D11 y D14; decisiones de Dan del 03/11 (la campaña activa desde el primer turno; a todo inbound sin palabra clave se le pregunta si viene por la campaña).
- **Depende de:** `segment-knowledge-v1` (PR-A, E-311: `[[agente.segmentos]]`, la vigencia y el nombre del archivo de cada segmento) y `segment-labels-v1` (E-316: el patrón del macro que solo suma la etiqueta). Construye su §3.
- **Migración:** `20261103001500_segment_entry_answers_v1.sql`.

## Para qué

Una campaña (Black November de ATT1) es, adentro del producto, un segmento: un conocimiento extra que el agente recibe mientras la conversación tiene la etiqueta de la campaña. Dan, el 03/11: la campaña tiene que estar activa **desde el primer turno** del agente. Brayan, en GHL, lo hacía con la palabra clave del primer mensaje (el botón de WhatsApp de la landing lleva un texto prellenado). Este contrato suma las dos formas de entrada que faltaban:

| Forma | Quién pone la etiqueta | Cuándo |
|---|---|---|
| La palabra clave del primer mensaje | el bridge, por el macro del segmento | antes de armar el pedido al agente de ese turno |
| La respuesta a la pregunta de entrada | el bridge, cuando el agente devuelve `campana_respuesta: "si"` | después del turno; el bloque entero va desde el turno siguiente |

Las otras formas siguen como estaban: el motor antes del envío y la herencia (`segment-labels-v1`), y el macro a mano.

## 1. La declaración

En `[[agente.segmentos]]` del manifiesto, dos campos opcionales (los dos piden `macro_etiqueta`):

```toml
[[agente.segmentos]]
clave = "black-november-2026"
etiqueta = "black-november"
conocimiento = "conocimiento/segmentos/black-november-2026-v1.toml"
prioridad = 10
macro_etiqueta = 57
palabras_clave = ["Black November"]
pregunta_entrada = "¿Nos escribes por la promoción de Black November?"
```

- `palabras_clave`: de 1 a 8 palabras clave con las que esperamos que la persona empiece la conversación (decisión de Dan del 03/11: **no se compara el texto del botón**, se detecta la palabra clave). Se guardan **normalizadas** (sin acentos ni mayúsculas, la puntuación y los espacios de más como un espacio) y se buscan **contenidas en cualquier parte del primer mensaje**, como palabras enteras: «Hola! Vengo por BLACK   NOVEMBER 🖤» y «black-november porfa» traen `black november`; «blacknovember» no. Dos palabras clave que normalizan igual, en el mismo segmento o en dos, no cargan.
- `pregunta_entrada`: una línea de hasta 200 caracteres. Es lo que el agente pregunta, tal cual. Dos segmentos con la pregunta y vigencias que se pisan no arrancan (D5: una sola campaña pregunta a la vez).
- La vigencia y el nombre (`cabecera.nombre`) salen del archivo del segmento (PR-A). Los precios por país no van acá: van en el conocimiento de la campaña.

## 2. La palabra clave

En el turno del agente, con la historia canónica de Chatwoot ya leída y **antes de armar el pedido**:

1. Si la historia está completa (se leyeron menos mensajes que el tope de la lectura) y el primer mensaje de la persona en la conversación (desde el último `/nuevo`) es parte de este turno, y contiene una palabra clave de un segmento **vigente** cuya etiqueta la conversación no tiene:
   - se pone la etiqueta por el macro del segmento (`ProductLabeler`: verifica que el macro solo sume esa etiqueta, es idempotente y nunca lanza);
   - se suma la etiqueta a las del turno y se vuelve a calcular la entrada `segments`: **la primera respuesta del agente ya lleva el bloque de la campaña** (`agent_replies_before = 0` en la procedencia).
2. Solo el primer mensaje (D14). Quien cambió el texto prellenado entra por la pregunta.
3. Lo que puso se recuerda por conversación (hasta 4.096): el macro corre en un job de Chatwoot y el webhook siguiente puede no traerla todavía.

Log: `segment_label_by_keyword conversation segment label outcome`.

## 3. La pregunta de entrada

Con el segmento de la pregunta **vigente**, a una conversación que:

- no tiene la etiqueta de esa campaña (ni la de la palabra clave de este turno);
- **la abrió la persona**: el primer mensaje público (desde el último `/nuevo`, sin actividades, notas privadas ni la confirmación de `/nuevo`) es un entrante del contacto. Una conversación que abrió una plantilla nuestra (carrito, pago rechazado, primer contacto) no lleva la pregunta;
- empezó **durante** la vigencia (el primer mensaje de la persona es de `desde` en adelante): a una conversación de antes no se le pregunta en el medio;
- no tiene respuesta registrada para esa campaña;

el turno suma un **aviso de campaña** como último `system`, después del base y de los segmentos:

```
# Campaña vigente: <nombre>
Hay una campaña vigente: <nombre>. Esta persona escribió por su cuenta y no sabemos si viene por ella. Pregúntale una sola vez: «<pregunta_entrada>». Que sea la única pregunta de esa respuesta. Hasta que conteste, no des precios, montos, descuentos ni plazos, ni los de siempre. Cuando conteste, suma a tu JSON "campana_respuesta": "si" o "no". Si no contesta claro, no la sumes.
```

Va en cada turno de esa conversación hasta que haya respuesta (el agente ve en la historia que ya preguntó). «Una sola pregunta por respuesta» es por el validador de la propuesta, que rechaza un `reply` con más de un `?`.

A una clienta también se le pregunta (D11). Con la base caída no se pregunta (`segment_entry_answers_unavailable`): mejor no preguntar de más.

### La respuesta

- **El procesador de Hermes** (con el flag, `campaign_answers=True`) saca `campana_respuesta` de la propuesta **antes** de validarla y la guarda aparte en el resultado (`campaign_answer`). La propuesta queda con la forma de siempre para todo el que la lee. `"si"` o `"no"` se guardan; otro valor (`null`, `"tal vez"`) no invalida la respuesta y no se guarda nada. Sin el flag, una clave de más invalida la propuesta, como hoy.
- **El bridge**, después del turno, solo si el turno llevaba el aviso:
  - `"si"`: la registra en la base y pone la etiqueta por el macro. Desde el turno siguiente va el bloque entero (lo recuerda mientras el macro no llegó al webhook).
  - `"no"`: la registra en la base (D4). No se vuelve a preguntar en esa conversación.
  - Nada o un valor que no es `si`/`no`: no se registra nada.
- Una respuesta registrada no se pisa: la primera queda (`replayed`).

Log: `segment_entry_answer conversation segment answer outcome=recorded|replayed|record_failed:<tipo> label=<lo que dijo el macro>|none`.

## 4. La base (`20261103001500_segment_entry_answers_v1.sql`)

- `segment_entry_answers`: una fila por (cuenta, inbox, conversación de Chatwoot, clave del segmento), con `answer` (`si`/`no`) y `answered_at`. Sin teléfono, mail, nombre ni texto. RLS y sin permisos para ningún rol de la API.
- `record_segment_entry_answer_v1(account, inbox, conversation, segment_key, answer, now)` → `(outcome, answer)`: `recorded`, o `replayed` con la que ya estaba.
- `get_segment_entry_answers_v1(account, inbox, conversation)` → `(segment_key, answer)`.
- Solo `service_role` ejecuta las dos. Correrla dos veces aborta con 55000; una entrada inválida, 22023.

## 5. Sin el flag o sin campaña vigente

El pedido a Hermes es **byte a byte el de hoy**: sin campaña vigente no se lee nada nuevo, y sin el flag el turno no corre nada de esto (test `test_without_the_flag_or_a_campaign_in_force_the_request_is_byte_for_byte_the_same`). Con el aviso, el pedido es el de hoy más un `system` antes del contexto.

## 6. La procedencia

El aviso va en `segments` de la procedencia del turno con `kind: "aviso_de_campana"` (clave y versión del segmento, sha del texto del aviso, `agent_replies_before`). Así «entró por la pregunta» se cuenta aparte de «llegó tarde» (E-319 §1.2 y §5).

## 7. El arranque (falla cerrado)

`CAMPAIGN_INBOUND_ENTRY_ENABLED` pide: manifiesto, `AGENT_SEGMENTS_ENABLED` con el catálogo cargado, el cliente de control de Chatwoot, `CHATWOOT_ACCOUNT_ID` y `CHATWOOT_INBOX_ID`, y al menos un segmento con `palabras_clave` o `pregunta_entrada`. Con una pregunta, además el cliente de Supabase y un procesador que lea `campana_respuesta` (el real, con el flag). El macro no puede ser el de la pausa, la reanudación, la baja ni el de `cliente`. Es una capacidad portable (por instancia, solo prenderlo).

## 8. Cómo se prende en ATT1

1. El segmento de la campaña cargado y aprobado (PR-A), con `macro_etiqueta` (un macro de la cuenta 2 que solo suma la etiqueta).
2. En `[[agente.segmentos]]`, `palabras_clave` con el texto del botón de la landing y, si se quiere la pregunta, `pregunta_entrada`.
3. La migración `20261103001500` aplicada y la imagen desplegada.
4. `CAMPAIGN_INBOUND_ENTRY_ENABLED=true` (con `AGENT_SEGMENTS_ENABLED=true`).
5. Prueba con el teléfono de un aprobador: `/nuevo` y el texto del botón → `segment_label_by_keyword … outcome=added` y `segment_context key=<clave>` en el mismo turno; `/nuevo` y «hola» → el agente pregunta; «sí» → `segment_entry_answer … answer=si outcome=recorded label=added`.

## 8b. El camino del Automation Expert (encargo siguiente)

Dan, 03/11: la palabra clave la tiene que poder configurar el Automation Expert, y la automatización la detecta. Hoy el AE cambia configuración por un solo camino, el ciclo de `flow-change-cycle-v1`: `propose_flow_change` arma una propuesta con ítems sobre flujos del almacén, `validate_flow_change` corre las 17 reglas, el sí lo da un aprobador en la página `flow_approval` (Sign in with Slack) y publicar es un paso atómico en la base. Diseño: la palabra clave entra por ese mismo camino como **un flujo de entrada**, sin un ciclo nuevo:

```yaml
dispara: {evento: primer_mensaje, palabra_clave: "Black November"}
pasos:
  - etiquetar: black-november     # la etiqueta de un segmento con macro (E-316)
```

- El esquema (`flow-definition-v1`) suma el disparador `primer_mensaje` con `palabra_clave` (1 a 8, normalizadas), que solo admite un paso `etiquetar` de segmento; el validador suma sus reglas (palabra clave repetida con otro flujo activo, segmento vencido, etiqueta sin macro). La página de aprobación lo muestra como una burbuja con la palabra clave y la etiqueta.
- El bridge lee, con caché corta, los flujos `primer_mensaje` activos de la instancia (la misma lectura del almacén que usa `/flows`) y los suma a las palabras clave del manifiesto en `CampaignInboundEntry.turn`; lo demás de este contrato no cambia. La admisión nativa nunca recibe este disparador (no hay evento asíncrono): una migración de forma lo deja cerrado en `_flow_engine_native_unsupported`.
- Lo que ve el AE: `list_flows` trae los flujos de entrada; la biblioteca suma cómo escribirlo. El SOUL del AE está al tope: va a la biblioteca.
- Tamaño: **1 a 1,5 días de cadena** (esquema y validador, migración de forma, lectura del bridge con caché, página, biblioteca y lectura del AE, tests y PGlite). Va como encargo propio después de este PR; la pregunta de entrada puede seguir en el manifiesto o pasar por el mismo camino en una segunda vuelta.

## 9. Decisiones (con su default)

- **D-E317-1.** Las palabras clave y la pregunta van en `[[agente.segmentos]]` del manifiesto (como diseñó `segment-labels-v1` §3), no en el archivo del segmento: el chequeo del alta (PR-B) tiene su propio lector cerrado del archivo. La pregunta, por lo tanto, no pasa por el juez.
- **D-E317-2.** «Primer mensaje» = el primer entrante de la persona en la conversación desde el último `/nuevo`, visto en la historia canónica; con la historia incompleta no hay primer mensaje.
- **D-E317-3.** Inbound = el primer mensaje público de la conversación es de la persona (una plantilla nuestra antes = no inbound).
- **D-E317-4.** La pregunta va solo a conversaciones que empezaron durante la vigencia.
- **D-E317-5.** El aviso va en cada turno hasta que haya respuesta; el texto pide preguntar una sola vez.
- **D-E317-6.** Se guardan el `si` y el `no`; la primera respuesta queda.
- **D-E317-7.** Un valor de `campana_respuesta` que no es `si`/`no` se descarta sin invalidar la propuesta.
- **D-E317-8.** Con la base caída, no se pregunta en ese turno.
- **D-E317-9.** Un solo flag para la palabra clave y la pregunta. Solo la palabra clave (sin `pregunta_entrada`) no necesita la base.
- **D-E317-11.** Mientras se pregunta, el aviso le prohíbe al agente cotizar (ni el precio de siempre, USD 47 en ATT1): el base v15 dice que sin el bloque `# Segmento:` no hay campaña, y una cotización normal a quien venía por la campaña es el error que la pregunta existe para evitar.
- **D-E317-10.** Se toman todas las palabras clave de segmentos vigentes que coincidan (si dos campañas comparten palabra clave, el manifiesto no carga).
- **D-E317-13.** La palabra clave cuenta **en cualquier parte** del primer mensaje, no solo al principio: un «Hola! Vengo por Black November» o un botón con saludo antes tienen que entrar. El costo es un falso positivo raro (alguien que nombra la campaña sin venir por ella), que solo le suma el bloque de la campaña.
- **D-E317-14.** Hoy las palabras clave se declaran en el manifiesto (PR de instancia). Que las configure el Automation Expert es el encargo siguiente (ver «El camino del Automation Expert»).

## 10. Las pruebas

- `tests/test_campaign_inbound_entry.py`: la normalización, la palabra clave (primer mensaje, recordada, fuera de vigencia, historia incompleta, etiqueta ya puesta), quién abrió la conversación sobre tres historias capturadas, la pregunta (inbound, siguientes turnos, plantilla nuestra, conversación de antes, `no`/`si` de la base, base caída, clienta), la respuesta (`si`, `no`, sin respuesta clara, sin aviso, base caída) y la declaración del manifiesto.
- `tests/test_campaign_inbound_entry_bridge.py`: el turno por el webhook (la etiqueta antes del agente y el bloque en la primera respuesta, el aviso último, `si`/`no` del agente, plantilla nuestra), el pedido byte a byte sin el flag o sin campaña vigente, Hermes y `campana_respuesta`, la procedencia, el cliente de Supabase y el arranque.
- `tests/test_segment_entry_answers_migration.py` y `tests/sql/followup_engine/validate_segment_entry_answers.mjs` (PGlite, la cadena entera).
- **Deuda:** no hay captura de un primer mensaje real con el texto de un botón de campaña, ni de una salida de Hermes con `campana_respuesta`.
