# Contrato: segmentos de conocimiento por etiqueta v1

- **Fecha:** 2026-11-03.
- **Diseño:** E-310, «Segmentos: una etiqueta le abre al agente un conocimiento extra» (vault del OS, `productos/soporte-infoproductores/documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md`), con los defaults D1 a D15.
- **Código:** `src/bridge/segment_knowledge.py` (esquema, render, catálogo, selección por turno), `src/bridge/instance_manifest.py` (`[[agente.segmentos]]`), `src/bridge/hermes.py` (`extra_system`), `src/bridge/app.py` (flag, entrada, `/ready`, logs), `src/bridge/instance_cli.py` (`setter validate`).
- **Esquema:** `segment-knowledge-v1`. Entrada del agente: `shadow-context-v6`.

## Qué es

Un **segmento** es una etiqueta de Chatwoot con un archivo de conocimiento aprobado y una vigencia. Si la conversación tiene la etiqueta y el segmento está vigente, el agente comercial recibe en ese turno un mensaje `system` más, **después del conocimiento base**, con el render del segmento. El SOUL común no cambia (D4): el preámbulo lo pone el producto.

Sirve para una campaña (precio, cupón, bonos, asesoramiento) que vale solo para las personas que entraron por ella, sin tocar el conocimiento base de la aliada ni su tope.

## Dónde vive y cómo se declara

En el repo de la instancia, al lado del conocimiento base (D1): `conocimiento/segmentos/<clave>-v<N>.toml`. Se declara en el manifiesto con `[[agente.segmentos]]` (`clave`, `etiqueta`, `conocimiento`, `prioridad` y, opcional, `macro_etiqueta`). La referencia de esos campos está en `docs/referencia-manifiesto.md`. La vigencia va **en el archivo** y no en el manifiesto (D2): la aprueba quien aprueba el contenido, y cambiar una fecha es una versión nueva.

## El archivo

Cerrado, como el base: no se acepta ninguna clave fuera de esta tabla.

| Tabla | Obligatorias | Opcionales |
|---|---|---|
| `[cabecera]` | `segment_version` (entero ≥ 1), `estado` (`borrador` o `aprobado`), `ally_ref`, `clave` (slug), `nombre` (titula el bloque) | `aprobado_por`, `aprobado_el` (fecha; los dos obligatorios si está `aprobado`), `chequeo_sha` (sha256 en hexadecimal del reporte del chequeo del alta) |
| `[vigencia]` | `desde`, `hasta`: fecha y hora **con zona** (`2026-11-20T00:00:00-06:00`), `desde < hasta` | |
| `[instruccion]` (opcional) | | `texto` (hasta 600 caracteres): cómo usar el bloque en esta campaña |
| `[oferta]` (opcional, no vacía) | | `producto` (clave de un producto del conocimiento base, solo si el base tiene `[[productos]]`), `precio` y `moneda` (juntos), `precio_antes` (con `precio`), `cupon` (código), `incluye` (lista), `garantia`, `medios` (lista), `reemplaza` (lista de `precio`, `moneda`, `garantia`, `incluye`, `medios`, cada uno presente en la oferta) |
| `[asesoramiento]` | `items` (no vacía): qué recomendar y a quién | |
| `[se_puede_decir]`, `[no_confirmado]`, `[promesas_prohibidas]` (opcionales) | `items` (no vacía) | |
| `[[faq]]` | `pregunta`, `respuesta`, `fuente` | |
| `[[preguntas_del_alta]]` | `pregunta`, `respuesta`, `fuente` | lo que contestó el equipo en el alta; es registro y **no va al render** |

`reemplaza` declara lo que el segmento cambia **a propósito** respecto del base (D14). El render lo dice («Cambia a propósito respecto del conocimiento general: …»). Una diferencia que no está en `reemplaza` es una contradicción: la marca el chequeo del alta (PR-B).

## Reglas de carga

- **El bridge solo carga segmentos `aprobados`.** Con `AGENT_SEGMENTS_ENABLED`, un segmento declarado en borrador, ilegible o inválido **no deja arrancar** al bridge. `setter validate` lee también los borradores, con un aviso.
- **El archivo es el que declara el manifiesto:** `cabecera.clave` es la `clave` de la declaración, `ally_ref` es el del manifiesto y el archivo se llama `<clave>-v<segment_version>.toml`.
- **Topes (D5):** un segmento renderizado pesa como máximo **8.000 bytes**; un segmento con el base no puede pasar los **40.000 bytes** del turno; como máximo **2 segmentos por turno**.
- **Convivencia (D6):** dos segmentos con vigencias que se pisan no pueden tener la misma prioridad, ni sumar con el base más de 40.000 bytes. Si pasa, el bridge no arranca. Así el recorte por tamaño del turno no debería pasar en producción.
- **Sin secretos:** se reusa el filtro del conocimiento base (claves `sk-`, tokens de Slack, GitHub o Meta, JWT, hex largos) sobre el render.
- **El flag exige manifiesto y conocimiento base** (`INSTANCE_MANIFEST_PATH` y `COMMERCIAL_KNOWLEDGE_ENABLED`): los segmentos se apilan sobre el base y se miden contra él. Johanna (sin manifiesto) no arranca con el flag.
- **El chequeo del alta (PR-B).** `segment_knowledge.verificar_chequeo` es el punto donde el cargador va a exigir que `chequeo_sha` sea el del reporte del chequeo determinista sobre el contenido actual. En esta versión no hace nada: el campo se parsea y se valida su forma. Recibe el segmento, la carpeta de la instancia, el conocimiento base y los otros segmentos.

## El render

Determinista: el mismo archivo y la misma zona de la aliada (`instancia.zona_horaria`) dan el mismo texto y el mismo `sha256`. Orden fijo:

1. `# Segmento: <nombre> (vigente hasta el <dd/mm HH:MM>, hora de la aliada)`. `hasta` es exclusivo, así que se muestra un segundo antes: `hasta = 2026-12-01T00:00:00-06:00` se lee «hasta el 30/11 23:59».
2. El preámbulo fijo del producto: «Esta persona está en esta campaña. Lo de este bloque vale para ella y, donde dice algo distinto del conocimiento general, manda este bloque. No cambia cómo respondes, el link de pago ni cuándo derivar.»
3. `## Cómo usar este bloque` (si hay `[instruccion]`), `## Oferta de la campaña` (si hay `[oferta]`), `## Asesoramiento`, `## Lo que sí se puede decir`, `## No confirmado`, `## Promesas prohibidas` y `## Preguntas frecuentes de la campaña`, cada uno si tiene algo.

## En cada turno (`AGENT_SEGMENTS_ENABLED`, apagado por defecto)

Portable: la regla es del producto y por instancia solo se prende.

1. Las etiquetas salen de `conversation.labels` del webhook, como la de `cliente` (`customer_status.conversation_labels`). Vienen en cada mensaje de la persona, sin importar quién las puso (un macro, una persona, más adelante el motor). Son etiquetas **de la conversación**, no del contacto (D9).
2. Se cruzan con los segmentos declarados **vigentes ahora**: `desde ≤ ahora < hasta`, calculado en cada turno, sin redesplegar (D7).
3. Se ordenan por `prioridad` (menor primero, a igualdad por `clave`) y van **como máximo 2**; uno que con el base y los anteriores pasa los 40.000 bytes no va.
4. **La entrada del agente** suma `segments` (`shadow-context-v6`), solo si va alguno:

   ```json
   "segments": [{"key": "campana-de-prueba", "version": 1}]
   ```

5. **El pedido a Hermes** lleva un `system` por segmento, en ese orden, **después** del conocimiento base y antes del mensaje `user`. Sin segmentos (o con el flag apagado) el pedido es **byte a byte** el de antes. La clave de idempotencia y el digest del resultado salen del delivery y no cambian.
6. **La procedencia del turno** (`agent_turn_provenance.context_added`) suma `segments` con `key`, `version`, `sha256` y `agent_replies_before` de cada segmento que fue (ver «La etiqueta antes del primer turno»). Sin segmentos, `context_added` es el de antes.

La etiqueta queda en Chatwoot como historia: el bridge no la pone ni la saca.

## El cupón de la campaña en el link (PR-A2)

Un segmento que **va en el turno** (etiqueta, vigente, dentro de los topes) y trae `[oferta].cupon` cambia dos cosas, y solo esas. Sin segmento con cupón (flag apagado, sin la etiqueta, vencido, por empezar o sin `cupon`) las dos salen como antes.

- **El link del agente** (`send_payment_link`) sale con `&offDiscount=<cupón>` al final del link que emitió la base, con el mismo armado que el cupón fijo de la instancia (`payment_link_coupon.payment_link_with_coupon`): el texto, el botón y lo escrito en Chatwoot iguales; la fila de la base guarda el link sin cupón y la atribución por `sck` no cambia. El cupón del segmento **reemplaza** al fijo (`PAYMENT_LINK_COUPON_CODE`) y **no depende** del toque de recuperación (`PAYMENT_LINK_COUPON_RECOVERY_ONLY`); con ese flag, `payment_link_action.coupon_applied` pasa a `true`. Con dos segmentos con cupón en el turno, va el del primero (menor prioridad). Si el link de la base ya trae un `offDiscount` (cupón personal), manda ese, como con el fijo.
- **El cupón del carrito** (E-267, `RECOVERY_COUPON_BY_REPLY_ENABLED`): no se pide la renovación por respuesta y la entrada no lleva `recovery_coupon`, igual que con una clienta (`customer_status`). Se decide después de la palabra clave de la campaña (`campaign-inbound-entry-v1`), así que también frena con la etiqueta que puso la palabra clave en ese mismo turno.

No cambia lo que mandan los flujos: una plantilla del carrito con cupón que salga del motor sigue saliendo. El cupón se declara en `[oferta].cupon` y no va en `reemplaza` (el base no tiene un campo de cupón; ver `segment-check-v1`).

## La etiqueta antes del primer turno

**La etiqueta tiene que estar puesta antes del primer turno del agente en la conversación** (Dan, 2026-11-03): un inicio de conversación sin el segmento deja en el historial respuestas que el agente dio sin él, y eso ensucia lo que lee en los turnos siguientes aunque después se sume la etiqueta.

- **Cómo se garantiza:** no lo garantiza esta versión. La etiqueta la tiene que poner quien abre la conversación de la campaña antes de que la persona conteste: el paso `etiquetar` del flujo que manda la plantilla de la campaña, o una regla por palabra clave del primer mensaje. Eso es PR-C, no este contrato. Un macro a mano después del primer mensaje llega tarde.
- **Cómo se detecta:** cada turno con un segmento anota en la procedencia, junto a su clave, versión y sha, `agent_replies_before`: cuántas respuestas del agente ya tenía la conversación antes de este turno (después del último `/nuevo`). **Un segmento llegó tarde a una conversación si su primer turno con ese segmento tiene `agent_replies_before > 0`.** Se mide sobre `agent_turn_provenance` (cada turno queda registrado), sin estado nuevo en el bridge. Además, cada turno con un segmento y respuestas del agente antes deja el warning `segment_after_agent_replies key=… version=… agent_replies_before=… conversation=…`.
- **La etiqueta del contacto:** el webhook `message_created` trae solo `conversation.labels` (medido en las tres capturas del repo: conversaciones 158 y 200 del inbox 9 y 536 del inbox 11). Si WhatsApp abre una conversación nueva para la misma persona, la etiqueta de la anterior no pasa sola. Leer las etiquetas del contacto, o las de sus otras conversaciones (`GET /contacts/{id}/conversations`, que ya tiene captura), es una llamada más a Chatwoot por turno y va con PR-C.

## `/ready`

Con el flag, `agent_segments` lista cada segmento declarado como `clave:vN:sha256:estado`, separados por coma, con `estado` = `por_empezar`, `vigente` o `vencido` calculado en el momento del pedido; `none` si no hay ninguno. Sin el flag, la clave no aparece y `/ready` no cambia.

## Los logs (`LOGGED_INFO_EVENTS`)

- `segment_context key=… version=… sha256=<12> conversation=…`: un segmento que fue en el turno.
- `segment_inactive key=… version=… reason=expired|not_started conversation=…`: la conversación tiene la etiqueta y el segmento no está vigente.
- `segment_dropped_max_per_turn …` y `segment_dropped_size …` (warning): un segmento vigente que no fue por los topes del turno.
- `segment_after_agent_replies key=… version=… agent_replies_before=… conversation=…` (warning): un segmento que fue en una conversación donde el agente ya había respondido.
- `segment_link_coupon key=… conversation=…`: el link del agente salió con el cupón de ese segmento (nunca el código).
- `segment_recovery_coupon_skipped key=… conversation=…`: con ese segmento no se ofreció el cupón del carrito.

Los dos de PR-A2 todavía no están en `LOGGED_INFO_EVENTS`: se ven en el log del proceso, no en el del contenedor.

Nunca el teléfono, el mail ni el texto de los mensajes.

## Lo que no hace (todavía)

- **El chequeo del alta** (`setter segmento chequear`: contradicciones y huecos, `chequeo_sha` obligatorio para cargar): PR-B.
- **El link por oferta del segmento** (una campaña con otro código de oferta y no un cupón): en la v1 el segmento lo dice en `[instruccion]` y el agente deriva para el link (D8).
- **Poner la etiqueta desde el motor** (`etiquetar` con las etiquetas de los segmentos, `macro_etiqueta`, la regla `segment_label_outside_window`) y la lectura del AE (`/segments`, `allowed_labels`): PR-C. Hoy la pone el equipo con un macro de Chatwoot.
- `cliente` no es un segmento: sigue por `customer_status`, con sus frenos (D13).

## Versiones

Un cambio en un segmento (incluida una fecha) es un archivo nuevo (`<clave>-v2.toml`) con `segment_version = 2`, y el manifiesto pasa a apuntarlo. El archivo anterior queda en el repo de la instancia como historia. Un segmento vencido se saca del manifiesto en el PR de instancia siguiente.
