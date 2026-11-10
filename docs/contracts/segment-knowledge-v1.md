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
| `[oferta]` (opcional, no vacía) | | `producto` (clave de un producto del conocimiento base, solo si el base tiene `[[productos]]`), `precio` y `moneda` (juntos), `precio_antes` (con `precio`), `cupon` (código), `codigo_oferta` (la oferta de Hotmart de la campaña, `[A-Za-z0-9]{4,32}`, declarada en el manifiesto para el producto del segmento; E-410), `frenar_descuento_carrito` (`true` o `false`, por defecto `false`; E-410), `incluye` (lista), `garantia`, `medios` (lista), `reemplaza` (lista de `precio`, `moneda`, `garantia`, `incluye`, `medios`, cada uno presente en la oferta) |
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
- **El chequeo del alta (PR-B, E-337).** `segment_knowledge.verificar_chequeo` llama a `segment_check.verificar_chequeo` (contrato `segment-check-v1`) y convierte su `ChequeoError` en `SegmentError`: un segmento `aprobado` sin `cabecera.chequeo_sha`, o con uno que no es el que da `calcular_chequeo_sha` sobre el contenido actual, el render del base y los otros segmentos que se pisan, **no deja arrancar** al bridge, como uno en borrador. Cambiar el conocimiento base cambia la huella: el segmento vuelve al chequeo. `setter validate` hace la misma verificación y lo marca como error antes del despliegue. Recibe el segmento, el conocimiento base y los otros segmentos.

## El render

Determinista: el mismo archivo y la misma zona de la aliada (`instancia.zona_horaria`) dan el mismo texto y el mismo `sha256`. Orden fijo:

1. `# Segmento: <nombre> (vigente hasta el <dd/mm HH:MM>, hora de la aliada)`. `hasta` es exclusivo, así que se muestra un segundo antes: `hasta = 2026-12-01T00:00:00-06:00` se lee «hasta el 30/11 23:59».
2. El preámbulo fijo del producto: «Esta persona está en esta campaña. Lo de este bloque vale para ella y, donde dice algo distinto del conocimiento general, manda este bloque. No cambia cómo respondes, el link de pago ni cuándo derivar.» Con `[oferta].codigo_oferta` (E-410) va una variante: «… No cambia cómo respondes ni cuándo derivar. El link de pago lo arma el sistema con la oferta de esta campaña.», y la oferta suma la línea «El enlace de pago de esta campaña es el de su oferta: pedilo como siempre.» (el código no se le dice al agente). Sin el campo, el render y su `sha256` son los de siempre, byte a byte. `frenar_descuento_carrito` no cambia el render.
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
7. **A quien se dio de baja no le va** (E-355). Con la misma señal que la entrada a una campaña (`prior_opt_out`, la baja anterior de esa persona), el turno se arma sin `segments`, como si la conversación no tuviera la etiqueta: sin el bloque y sin el cupón de la campaña en el link. Se le contesta normal (el SOUL común, D10) y queda `segment_opted_out_skipped`. Vale para todo segmento, del manifiesto o de la base. Solo se ve con el flag prendido y un segmento vigente con la etiqueta.

La etiqueta queda en Chatwoot como historia: el bridge no la pone ni la saca.

## El cupón de la campaña en el link (PR-A2)

Un segmento que **va en el turno** (etiqueta, vigente, dentro de los topes) y trae `[oferta].cupon` cambia dos cosas, y solo esas. Sin segmento con cupón (flag apagado, sin la etiqueta, vencido, por empezar o sin `cupon`) las dos salen como antes.

- **El link del agente** (`send_payment_link`) sale con `&offDiscount=<cupón>` al final del link que emitió la base, con el mismo armado que el cupón fijo de la instancia (`payment_link_coupon.payment_link_with_coupon`): el texto, el botón y lo escrito en Chatwoot iguales; la fila de la base guarda el link sin cupón y la atribución por `sck` no cambia. El cupón del segmento **reemplaza** al fijo (`PAYMENT_LINK_COUPON_CODE`) y **no depende** del toque de recuperación (`PAYMENT_LINK_COUPON_RECOVERY_ONLY`); con ese flag, `payment_link_action.coupon_applied` pasa a `true`. Con dos segmentos con cupón en el turno, va el del primero (menor prioridad). Si el link de la base ya trae un `offDiscount` (cupón personal), manda ese, como con el fijo. **El cupón es del producto de la campaña:** va solo si el link es de `[oferta].producto` o, sin `producto`, del principal; un link de otro producto sale como siempre (con el fijo, si la instancia lo tiene). Una instancia sin `[[productos]]` tiene un solo producto y ahí siempre va. Con varios productos, `coupon_applied` no se adelanta: el producto lo nombra el agente después.
- **El cupón del carrito** (E-267, `RECOVERY_COUPON_BY_REPLY_ENABLED`): no se pide la renovación por respuesta y la entrada no lleva `recovery_coupon`, igual que con una clienta (`customer_status`). Se decide después de la palabra clave de la campaña (`campaign-inbound-entry-v1`), así que también frena con la etiqueta que puso la palabra clave en ese mismo turno.

**Lo que el producto manda solo tampoco le ofrece otro cupón** (E-339, D6 de E-338). Un segmento con `[oferta].cupon` vigente, con la misma selección de un turno y sin mirar el producto, cambia lo que sigue. Un segmento sin `cupon` no cambia nada.

- **A1 del carrito** (`first_contact`, con `RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED`): sale igual, con la plantilla sin descuento del carrito, la misma caída de cuando el cupón no está listo: sin cupón y sin link emitido. Como la conversación todavía no se eligió, las etiquetas son las de todas las conversaciones del contacto en el inbox (`GET /contacts/{id}/conversations`). Si esa lectura falla, reintenta a los 5 minutos y, pasados los intentos de espera de A1, sale sin cupón: A1 no se pierde. Si la lista vino cortada (Chatwoot da 20 conversaciones, las más activas), decide con las que se leyeron. Log `segment_first_touch_without_coupon`.
- **A2 y Z del carrito** (`cart_second_touch`, `cart_last_touch`), con el motor o sin él: no salen. Las etiquetas salen primero de la lectura canónica de la conversación del caso y, si ahí no hay, de las conversaciones del contacto, como A1. Si esa lectura falla, reintenta tres veces, cada 15 minutos, y en el último intento decide con la conversación del caso: sin evidencia de la etiqueta el paso sale. Si ese último envío también falla, el caso queda abierto: es la excepción conocida `fallo_permanente_deja_el_caso_abierto`. Con Chatwoot caído del todo no se llega a estos reintentos: la reevaluación da `escalate` con `chatwoot_authority_unavailable`. Si la lista vino cortada no reintenta: decide con las conversaciones leídas más la del caso, frena solo con evidencia de la etiqueta y deja `segment_contact_labels_truncated`. La RPC `close_recovery_case_segment_coupon_v1` (migración `20261106000100`) cierra en una transacción el intento y la acción (`permanent_failed`), la secuencia (`completed`) y el caso (`cancelled`), con el motivo `segment_coupon_active`: un carrito nuevo de esa persona arranca limpio, por el legado, el motor o el nativo. Si otra cosa cerró el intento, la acción o el caso antes (una compra entre la reevaluación y el cierre) contesta `case_not_open` y no toca nada. Sin la RPC (flag prendido sin la migración) no sale nada, reintenta (`segment_coupon_close_unavailable`) y la acción termina cerrada con el caso abierto: la migración va antes de prender el flag.
- **El seguimiento con cupón fijo** (`ConversationFollowupSweeper`, el legado y el silencio nativo; es el que lleva `TIROIDES10`): mira las etiquetas de la conversación barrida. La candidata no se reserva y el resumen la cuenta como `segment_coupon_active`. Se vuelve a evaluar en cada barrido, así que si la campaña termina dentro de la ventana del seguimiento, sale. Si el cupón del barrido es el de la propia campaña, sale: no es un segundo cupón.
- **El botón «Quiero mi 10 %»:** no adelanta Z. El mensaje sigue el camino de siempre: la admisión y el agente, que contesta con el bloque del segmento.
- **La compra:** una compra de la misma oferta cierra el caso al entrar. Una por otra oferta no lo cierra al entrar (`20261019000300`): la reevaluación cancela el próximo toque con `purchase_detected_any_offer`, antes de este freno. Con el caso ya cerrado por el freno no queda ningún toque que cortar.

**No frena:** el pago rechazado (B1, B2 y Z), el carrito nativo ni los pasos nativos que no sean el silencio: un paso nativo con `boton.cupon` puede ser el cupón de la propia campaña. El silencio nativo sí entra (arriba). Tampoco borra un cupón personal ya sembrado: vence sin salir. La vista del Automation Expert (`_coupon_view`) no lee etiquetas: puede mostrar como próximo un seguimiento que el barrido va a saltear.

El cupón se declara en `[oferta].cupon` y no va en `reemplaza` (el base no tiene un campo de cupón; ver `segment-check-v1`).

**Una campaña sin cupón que frena igual (E-410, decisión de Dan del 2026-11-10).** `[oferta].frenar_descuento_carrito = true` hace que un segmento vigente **sin** `cupon` active los mismos frenos de arriba (A1 sin el 10 %, A2 y Z que no salen con el cierre del caso, el seguimiento con cupón fijo, el botón «Quiero mi 10 %» y la renovación por respuesta). Por defecto es `false`: sin el campo, o en `false`, una campaña sin cupón no frena nada, como antes. Gana el primer segmento del turno por prioridad que traiga `cupon` o el campo en `true`. El link del agente no lleva cupón por esto (`segment_link_coupon` sigue mirando solo `cupon`). Decide por las etiquetas, igual que E-339: tampoco frena el carrito nativo, y un carrito que recibe la etiqueta en el `before_send` de su propio A1 nativo (la entrada por oferta, `campaign-entry-v1`) sale con ese A1 tal cual.

## La oferta de la campaña en el link (E-410)

Con `AGENT_CAMPAIGN_OFFER_LINK_ENABLED` (apagado por defecto; exige `AGENT_SEGMENTS_ENABLED` y un manifiesto), un segmento que **va en el turno** y trae `[oferta].codigo_oferta` cambia la reserva del link del agente (`send_payment_link`):

- **La reserva** es `reserve_portable_checkout_issuance_for_offer_v1` (migración `20261110000500`) con el `funnel_ref` del producto del link (con un solo producto, el del principal), la oferta y la clave del segmento (`campaign_ref`). Sale con la oferta de la campaña, el mismo `sck` (`hermes~v1~<ULID>`, con el `sck` del formulario adelante si lo hay) y la misma fila en `checkout_link_issuances`, con `offer_resolution = 'campaign'`. Ver `johanna-payment-link-v2`.
- **De qué producto es:** la regla del cupón (D10 de E-338). Un link de otro producto sale como siempre.
- **Prioridad:** con dos segmentos con oferta en el turno, la del primero (D9 de E-408).
- **Contra la intención de la persona:** gana la campaña. La intención de su formulario sigue aportando el `sck` y el `fbclid`, y su oferta queda en `lead_offer_code` (D3 de E-408).
- **Sin la oferta:** si la oferta no está activa en el catálogo del funnel, la base contesta `blocked_campaign_offer` sin escribir nada y el turno se deriva (`payment_link_campaign_offer_unavailable`). Nunca la oferta por defecto (D2).
- **El cupón** no cambia: con `cupon` y `codigo_oferta` a la vez, el cupón va sobre la oferta de la campaña (que Hotmart lo acepte en esa oferta es configuración de Hotmart).
- **Lo que no cubre (D7):** A1, A2 y Z del carrito, el seguimiento con cupón, el botón del 10 % y la nota de derivación siguen con la oferta de siempre.
- **Sin el flag, o sin el campo:** la reserva y su pedido son los de siempre, byte a byte.

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
- `segment_opted_out_skipped key=… conversation=…`: la conversación tenía el segmento y no fue porque la persona se dio de baja (E-355).
- `segment_dropped_max_per_turn …` y `segment_dropped_size …` (warning): un segmento vigente que no fue por los topes del turno.
- `segment_after_agent_replies key=… version=… agent_replies_before=… conversation=…` (warning): un segmento que fue en una conversación donde el agente ya había respondido.
- `segment_link_coupon key=… conversation=…`: el link del agente salió con el cupón de ese segmento (nunca el código).
- `segment_link_coupon_other_product key=… product=… conversation=…`: el turno tenía un segmento con cupón y el link era de otro producto; salió sin ese cupón.
- `segment_recovery_coupon_skipped key=… conversation=…`: con ese segmento no se ofreció el cupón del carrito.
- `segment_link_offer key=… conversation=…`: el link del agente se reservó con la oferta de ese segmento (E-410; nunca el código).
- `segment_link_offer_other_product key=… product=… conversation=…`: el turno tenía un segmento con oferta y el link era de otro producto; salió como siempre.

- `segment_first_touch_without_coupon key=… action_id=…`: A1 salió sin cupón por ese segmento (E-339).
- `segment_contact_labels_truncated contact=… read=… outcome=frenado|sin_evidencia`: la lista de conversaciones del contacto vino cortada y se decidió con las leídas (E-339).
- `segment_cart_followup_skipped key=… step=… action_id=…`: A2 o Z no salió por ese segmento (E-339).
- `segment_coupon_case_closed action_id=… outcome=closed|case_not_open`: lo que contestó el cierre del caso (E-339).
- `segment_followup_coupon_skipped key=… conversation=…`: el seguimiento con cupón fijo no salió por ese segmento, una vez por conversación y por proceso (E-339).

`segment_recovery_coupon_skipped` también sale cuando el botón «Quiero mi 10 %» no adelanta Z.

Nunca el teléfono, el mail ni el texto de los mensajes.

## El segmento que nace como dato (`agent-campaigns-v1`, E-355)

Con `AGENT_CAMPAIGNS_ENABLED`, una campaña que el Automation Expert armó y un aprobador publicó entra al catálogo desde la base, con este mismo esquema y el mismo `chequeo_sha`. Lo que cambia es la regla del sha: una campaña de la base cuyo `chequeo_sha` no calza **no se sirve, avisa y el bridge sigue** (decisión de Dan del 2026-11-08). La regla de arriba (no arranca) queda para los archivos del manifiesto. Contrato: [agent-campaigns-v1.md](agent-campaigns-v1.md).

## Lo que no hace (todavía)

- **El chequeo del alta** (`setter segmento chequear`: contradicciones y huecos, `chequeo_sha` obligatorio para cargar): PR-B.
- **El link por oferta del segmento** (D8): una campaña con una oferta propia de Hotmart la declara en `[oferta].codigo_oferta`. Con `AGENT_CAMPAIGN_OFFER_LINK_ENABLED`, el link del agente se emite con esa oferta, con el mismo `sck` y la misma fila; si no se puede, se deriva (arriba). Sin el flag, como antes: se deriva por `[instruccion]`.
- **Poner la etiqueta desde el motor** (`etiquetar` con las etiquetas de los segmentos, `macro_etiqueta`, la regla `segment_label_outside_window`) y la lectura del AE (`/segments`, `allowed_labels`): PR-C. Hoy la pone el equipo con un macro de Chatwoot.
- `cliente` no es un segmento: sigue por `customer_status`, con sus frenos (D13).

## Versiones

Un cambio en un segmento (incluida una fecha) es un archivo nuevo (`<clave>-v2.toml`) con `segment_version = 2`, y el manifiesto pasa a apuntarlo. El archivo anterior queda en el repo de la instancia como historia. Un segmento vencido se saca del manifiesto en el PR de instancia siguiente.
