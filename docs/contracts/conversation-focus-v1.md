# Contrato: el producto en foco de una conversación (conversation-focus-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que van a construir MP-11 (el foco provisorio), MP-12 (el estado y el resolvedor) y MP-13 (el cambio en el turno y la guarda del link); no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `conversation-focus-v1`, versión `0.1.0` (propuesta).
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md`, MP-11 a MP-13 y A-0), el foco del diseño C (`multiproducto-diseno-c-libre.md` §3) con tres piezas del B (`multiproducto-diseno-b-producto-activo.md` §2: el campo `product` que ya existe, el flujo que escribió fija el producto, la preselección por alias) y dos del A (medir antes de construir; pedir el link de otro producto cambia el foco), según el veredicto de `multiproducto-comparacion.md` §3. Decisión D2 de Dan del 2026-11-13 (`decisiones/2026-11-13-multiproducto-decisiones-d1-d8.md`): el default, el sistema decide con señales y el agente avisa.
- **Depende de:** `product-card-v1` (lo que entra al turno para el producto en foco), `price-guard-v1` (comparte el segundo intento), `segment-knowledge-v1` y `agent-campaigns-v1` (la campaña como señal), `flow-definition-v1` (`flow_controls.product_ref`).
- **Código que lo va a implementar:** `src/bridge/product_focus.py` (nuevo: el resolvedor), `src/bridge/app.py` (el turno, el link), `src/bridge/hermes.py` (otros `extra_system` en el segundo intento), `src/bridge/native_silence.py` (el silencio toma el foco), `profiles/agente-comercial-comun/SOUL.md` (sección «Productos», condicionada al índice), una migración con el estado y su historial.
- **Flag:** `AGENT_PRODUCT_FOCUS_ENABLED`, apagado por defecto. Apagado, no se lee ni se escribe el foco, no hay índice y todo sale byte a byte como hoy. Sin productos además del principal, prendido tampoco cambia nada: el foco es siempre el principal.

## 1. Qué resuelve

Con varios productos en una instancia, el agente no puede recibir el conocimiento de todos en cada turno (el v9 de Maribel ya pesa 30.300 de 32.000 bytes). Recibe el conocimiento base de siempre, **un índice** de una línea por producto (nombre, sin precios) y **el conocimiento de un solo producto: el que está en foco**. Este contrato dice cómo se sabe cuál es, cómo cambia en medio de una charla y qué pasa con el link.

**Quién decide (D2): el sistema, con señales.** El agente no elige qué conocimiento recibe: avisa cuando la persona cambió de producto, con el campo `product` que ya existe en su salida (`src/bridge/hermes.py:218-246` en `262d2f9`), y el turno se repite una vez.

## 2. El estado

Tabla `conversation_product_focus`, una fila por (tenant, conversación de Chatwoot): `producto` (la clave; la del principal también), `campana` (la clave de la campaña que lo fijó, o null), `fuente` (la señal de §3), `fijado_por` (`sistema` | `agente` | `persona_del_equipo`), `desde`, `fijado_en_turno` (la entrega del turno). Historial append-only aparte, `conversation_product_focus_events` (de, a, fuente, turno, cuándo), sin datos personales, para el tablero (MP-22) y para auditar.

La etiqueta de Chatwoot es **espejo**, no fuente: Chatwoot no dice cuándo se puso una etiqueta, y con dos no habría forma de saber cuál manda. La excepción es la señal 1 de §3.

## 3. Las señales, en orden de precedencia

Se resuelve **una vez por turno**, antes de armar el pedido al modelo, en el mismo lugar donde hoy se arma el segmento.

| # | Señal | De dónde sale | Caduca |
|---|---|---|---|
| 1 | **Una persona del equipo lo fija**: etiqueta `foco:<clave>` puesta a mano en Chatwoot | etiquetas de la conversación (`conversation.labels` del webhook) | nunca, hasta otro cambio |
| 2 | **El cambio que pidió el agente** y el sistema ejecutó (§5), o **el link de un producto fuera de foco** (§6) | el turno anterior | hasta otro cambio |
| 3 | **El flujo que le escribió**: el último envío de un flujo nativo a la persona | `flow_controls.product_ref` del envío | 72 h desde el envío |
| 4 | **La campaña**: una etiqueta de segmento vigente cuya campaña tiene `oferta.producto` | `agent-campaigns-v1`, `campaign-inbound-entry-v1` | la vigencia de la campaña |
| 5 | **El nombre en el último mensaje** (preselección por alias, §4) | el texto de la persona | solo ese turno, si no lo confirma otra señal |
| 6 | **La actividad reciente**: formulario, carrito, pago rechazado o ficha de un producto (**no** la compra) | `person_activity` del contexto | 72 h; con dos productos en la ventana, el más reciente |
| 7 | **La landing o el formulario** de la conversación | `ProductRoutes.for_landing` (`src/bridge/product_routes.py:205`) | 7 días |
| 8 | **Nada**: el principal | el manifiesto | — |

**Reglas de pisado:**

- Una señal de más arriba pisa a una de más abajo.
- Una de igual o menor rango pisa al foco actual **solo si es más nueva** que él: si el agente cambió a Y hace dos turnos, un carrito viejo de X no la devuelve a X; un carrito de X de hace 5 minutos, o un flujo de X que le acaba de escribir, sí.
- **Una compra nunca mueve el foco.** Quien compró X y escribe suele venir por soporte de X (y el conocimiento de X con el estado de clienta alcanza) o por otro producto (y entonces cambia por las señales de siempre).
- Una señal que nombra una clave que no existe o un producto pausado o cerrado se ignora.

## 4. La preselección por alias

Si el último mensaje de la persona nombra **un solo** producto que no es el del foco, y no nombra al del foco, el bridge carga ese producto de entrada (fuente `alias`): ahorra la mayoría de las segundas llamadas. Con nombres de dos productos («compré El Protocolo, ¿Forever Young qué trae?») no adivina: decide el agente (§5).

En v1, los alias de un producto son su `nombre` y su `clave` de `product-card-v1` (y, para el principal, el nombre del manifiesto), comparados sin mayúsculas, acentos ni guiones. Alias declarados aparte serían un campo nuevo del mínimo: fuera de v1, solo si A-0 (§8) muestra que hacen falta.

## 5. El cambio de producto en el turno

1. Con el foco prendido y al menos un producto además del principal, la salida del agente puede traer `product` en cualquier decisión (hoy se usa con `send_payment_link`). Significa «el producto del que me habla la persona en su último mensaje». Ausente o `null` = no habla de ninguno en particular; no cambia nada. Si es obligatorio en todos los turnos o no, lo decide A-0 (§8) con la tasa de salidas inválidas medida.
2. `product` igual al foco, ausente o `null`: se entrega como hoy.
3. `product` es otra clave del índice: el bridge **no entrega** esa respuesta. Guarda el foco nuevo (fuente `agente`), arma los `system` con el conocimiento del producto nuevo y una línea de aviso («La persona pasó de X a Y. Lo dicho antes sobre precio o contenido de X no vale para Y.») y pide de nuevo, en el **segundo intento** que ya existe (`_MAX_PROPOSAL_ATTEMPTS = 2`, `hermes.py:55`), que pasa a poder llevar otros `extra_system`.
4. La segunda respuesta dice el producto recién cargado, o `null`: se entrega.
5. **Oscila** (la segunda respuesta nombra otro producto distinto): no hay tercer intento. Sale una **pregunta fija del sistema**, armada con los nombres del índice: «¿Me preguntás por X o por Y?». La respuesta de la persona vuelve a pasar por §3 y §4.
6. Una clave que no está en el índice invalida la propuesta, como hoy (`unknown_product`).
7. **El segundo intento es compartido** con la guarda de precio (`price-guard-v1` §4): si el primero falla por las dos cosas, el segundo lleva el conocimiento nuevo y la corrección de la guarda juntos. Nunca hay un tercer llamado.

Se registra en la procedencia del turno: `product_switch: {de, a, fuente, intento}`.

## 6. El link

- **Pedir el link de un producto fuera de foco cambia el foco**: si el agente devuelve `send_payment_link` con `product` distinto del foco, se trata como §5.3 (fuente `link`) y se repite el turno con el conocimiento de ese producto, en vez de derivar.
- **La guarda de producto ajeno** (MP-13, bloqueando desde que se prende el flag, D4): el link solo sale del producto en foco. Si después del segundo intento el link pedido sigue sin ser del foco, no sale y se deriva con `payment_link_product_unrouted` (el motivo que ya existe, `src/bridge/app.py:9586`). Un `reply` con `send_payment_link` que nombra otro producto del índice tampoco manda el link.
- Lo demás del link no cambia: la oferta la elige el bridge (la por defecto del producto o la de una campaña vigente de ese producto), nunca el agente.

## 7. Lo que el foco mueve fuera del turno

- **El silencio** (`native_silence.py`, hoy un `product_ref` por barrido): el barrido de un producto atiende las conversaciones cuyo foco es ese producto.
- **La clienta por producto** (MP-14): el cupón y los avisos miran `es_clienta_de(foco)`, no «compró algo». La regla «otro producto: solo si lo pide, sin descuento» sigue viviendo solo en el SOUL común (`SOUL.md:113`) mientras la venta cruzada esté apagada (`product-card-v1` §5, D7).
- **El tablero** (MP-22): cambios de foco por fuente, del historial.

## 8. Medir antes de construir el cambio (A-0)

Antes de MP-12 se mide con el arnés de conversaciones, con GLM 5.3 Flash, el SOUL común vigente, el v9 de Maribel y un índice de dos productos, sobre 30 conversaciones reales de Maribel y 20 sintéticas de un producto nuevo escrito con `product-card-v1`:

| Mide | Corte |
|---|---|
| (a) cambios de producto bien marcados con `product` | ≥ 90 % |
| (b) falsos cambios | ≤ 5 % |
| (c) salidas inválidas de más | ≤ 2 puntos más que hoy |
| (d) precio o contenido del producto nuevo dicho sin su conocimiento | ≤ 5 % |

También latencia y tokens de uno y dos llamados, y cuántas respuestas bloquearía la guarda de precio y cuántas salva el reintento. **Si pasa**, MP-13 como está en §5. **Si no**, MP-13 sin el cambio por `product`: solo las señales del sistema (§3, §4) y la pregunta fija cuando la persona nombra otro producto.

## 9. Errores y códigos

`unknown_product` (propuesta inválida, existe), `payment_link_product_unrouted` (derivación, existe), `product_focus_question` (la pregunta fija salió; log y procedencia), `product_focus_changed fuente=<…> conversation=<n>` (log, sin datos personales).
