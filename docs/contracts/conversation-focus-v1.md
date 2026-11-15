# Contrato: el producto en foco de una conversación (conversation-focus-v1)

- **Estado:** escrito antes del código (MP-01 del plan de multiproducto, E-553, 2026-11-13). Describe la interfaz que van a construir MP-11 (el foco provisorio), MP-12 (el estado y el resolvedor) y MP-13 (el aviso del agente, la pregunta fija y la guarda del link); no dice que nada esté construido, desplegado ni activado.
- **Contrato:** `conversation-focus-v1`, versión `0.2.0` (propuesta). La 0.2.0 deja vigente la rama B de §8 (el foco lo decide el sistema; `product` del agente es solo un aviso) y la preselección por los alias declarados de `product-card-v1` 0.2.0, por la decisión de Dan del 2026-11-14 sobre la medición A-0; historial en §10.
- **Diseño:** «Multiproducto: el plan de implementación» (vault del OS, `coordinacion/etapa-1/multiproducto-plan-de-implementacion.md`, MP-11 a MP-13 y A-0), el foco del diseño C (`multiproducto-diseno-c-libre.md` §3) con tres piezas del B (`multiproducto-diseno-b-producto-activo.md` §2: el campo `product` que ya existe, el flujo que escribió fija el producto, la preselección por alias) y dos del A (medir antes de construir; pedir el link de otro producto cambia el foco), según el veredicto de `multiproducto-comparacion.md` §3. Decisión D2 de Dan del 2026-11-13 (`decisiones/2026-11-13-multiproducto-decisiones-d1-d8.md`): el default, el sistema decide con señales y el agente avisa. Decisión de Dan del 2026-11-14 sobre A-0 (`decisiones/2026-11-14-multiproducto-a0-el-foco-lo-decide-el-sistema.md`): rama B de §8.
- **Depende de:** `product-card-v1` 0.2.0 (lo que entra al turno para el producto en foco y los alias de §4), `price-guard-v1` (comparte el segundo intento), `segment-knowledge-v1` y `agent-campaigns-v1` (la campaña como señal), `flow-definition-v1` (`flow_controls.product_ref`).
- **Código que lo va a implementar:** `src/bridge/product_focus.py` (nuevo: el resolvedor), `src/bridge/app.py` (el turno, el aviso, la pregunta fija, el link), `src/bridge/native_silence.py` (el silencio toma el foco), `profiles/agente-comercial-comun/SOUL.md` (sección «Productos», condicionada al índice), una migración con el estado y su historial.
- **Flag:** `AGENT_PRODUCT_FOCUS_ENABLED`, apagado por defecto. Apagado, no se lee ni se escribe el foco, no hay índice y todo sale byte a byte como hoy. Sin productos además del principal, prendido tampoco cambia nada: el foco es siempre el principal.

## 1. Qué resuelve

Con varios productos en una instancia, el agente no puede recibir el conocimiento de todos en cada turno (el v9 de Maribel ya pesa 30.300 de 32.000 bytes). Recibe el conocimiento base de siempre, **un índice** de una línea por producto (nombre, sin precios) y **el conocimiento de un solo producto: el que está en foco**. Este contrato dice cómo se sabe cuál es, cómo cambia en medio de una charla y qué pasa con el link.

**Quién decide (D2, y rama B de §8 desde el 2026-11-14): el sistema, con señales.** El agente no elige qué conocimiento recibe ni mueve el foco. Puede avisar de qué producto habla la persona con el campo `product` que ya existe en su salida (`src/bridge/hermes.py:218-246` en `262d2f9`), opcional y nunca obligatorio; ese aviso no repite el turno ni cambia el foco: si nombra un producto cuyo conocimiento no estuvo en el turno, sale la pregunta fija (§5).

## 2. El estado

Tabla `conversation_product_focus`, una fila por (tenant, conversación de Chatwoot): `producto` (la clave; la del principal también), `campana` (la clave de la campaña que lo fijó, o null), `fuente` (la señal de §3), `fijado_por` (`sistema` | `persona_del_equipo`; con la rama B el agente nunca fija el foco), `desde`, `fijado_en_turno` (la entrega del turno). Historial append-only aparte, `conversation_product_focus_events` (de, a, fuente, turno, cuándo), sin datos personales, para el tablero (MP-22) y para auditar.

La etiqueta de Chatwoot es **espejo**, no fuente: Chatwoot no dice cuándo se puso una etiqueta, y con dos no habría forma de saber cuál manda. La excepción es la señal 1 de §3.

## 3. Las señales, en orden de precedencia

Se resuelve **una vez por turno**, antes de armar el pedido al modelo, en el mismo lugar donde hoy se arma el segmento.

| # | Señal | De dónde sale | Caduca |
|---|---|---|---|
| 1 | **Una persona del equipo lo fija**: etiqueta `foco:<clave>` puesta a mano en Chatwoot | etiquetas de la conversación (`conversation.labels` del webhook) | nunca, hasta otro cambio |
| 2 | **La preselección confirmada**: un turno preseleccionó un producto por alias (§4) y la respuesta del agente lo confirmó (`product` igual a ese producto) y se entregó | el turno anterior | hasta otro cambio |
| 3 | **El flujo que le escribió**: el último envío de un flujo nativo a la persona | `flow_controls.product_ref` del envío | 72 h desde el envío |
| 4 | **La campaña**: una etiqueta de segmento vigente cuya campaña tiene `oferta.producto` | `agent-campaigns-v1`, `campaign-inbound-entry-v1` | la vigencia de la campaña |
| 5 | **El nombre en el último mensaje** (preselección por alias, §4) | el texto de la persona | solo ese turno; si el agente lo confirma, pasa a la señal 2 |
| 6 | **La actividad reciente**: formulario, carrito, pago rechazado o ficha de un producto (**no** la compra) | `person_activity` del contexto | 72 h; con dos productos en la ventana, el más reciente |
| 7 | **La landing o el formulario** de la conversación | `ProductRoutes.for_landing` (`src/bridge/product_routes.py:205`) | 7 días |
| 8 | **Nada**: el principal | el manifiesto | — |

**Reglas de pisado:**

- Una señal de más arriba pisa a una de más abajo.
- Una de igual o menor rango pisa al foco actual **solo si es más nueva** que él: si la persona pasó a Y por un alias confirmado hace dos turnos, un carrito viejo de X no la devuelve a X; un carrito de X de hace 5 minutos, o un flujo de X que le acaba de escribir, sí.
- **Una compra nunca mueve el foco.** Quien compró X y escribe suele venir por soporte de X (y el conocimiento de X con el estado de clienta alcanza) o por otro producto (y entonces cambia por las señales de siempre).
- Una señal que nombra una clave que no existe o un producto pausado o cerrado se ignora.

## 4. La preselección por alias

Es la señal que cubre lo que el agente no marca solo (A-0, §8: 7 de 15 cambios bien marcados cuando la persona no nombra el producto por su nombre). Si el último mensaje de la persona nombra **un solo** producto que no es el del foco, y no nombra al del foco, el bridge carga ese producto de entrada (fuente `alias`), antes de llamar al modelo. Con nombres de dos productos («compré El Protocolo, ¿Forever Young qué trae?») no adivina: queda el foco que había, y si el agente avisa del otro, sale la pregunta fija (§5).

**Los alias de un producto** son los declarados en `alias[]` del mínimo de `product-card-v1` (§2.1, desde la 0.2.0) más los implícitos: su `nombre` y su `clave`. El principal y los productos de `[[productos]]` del manifiesto tienen solo los implícitos (el nombre del manifiesto y la clave).

**Cómo se compara:** el mensaje y los alias pasan por la misma normalización de `product-card-v1` §2.1 (minúsculas, sin acentos, lo que no es letra ni dígito a espacio, espacios colapsados). Un alias aparece en el mensaje si está como **palabras completas** (rodeado de espacio o de los bordes del texto normalizado), no como pedazo de otra palabra. Si un alias aparece solo como parte de un alias más largo de otro producto que también aparece en el mismo lugar («protocolo» dentro de «protocolo black»), cuenta solo el más largo. Un producto pausado, cerrado o sin publicar no se preselecciona.

**Una negación también nombra:** «no quiero Forever Young» preselecciona Forever Young. En A-0, en seco, las 24 preselecciones fueron cambios bien cargados y ninguna falsa, pero la medición no corrió al agente con el bloque cargado. Lo corrige el turno: si el agente devuelve el principal, se entrega (§5.3); si devuelve otro producto cuyo conocimiento no estuvo, sale la pregunta fija (§5.4); si devuelve `null`, la preselección no se confirma y caduca con el turno.

## 5. El aviso del agente en el turno (rama B)

Vigente desde la decisión de Dan del 2026-11-14 (§8). El agente **no** cambia el foco ni provoca un segundo llamado por el producto.

1. Con el foco prendido y al menos un producto además del principal, la salida del agente **puede** traer `product` en cualquier decisión (hoy se usa con `send_payment_link`). Significa «el producto del que me habla la persona en su último mensaje». **Es opcional y nunca obligatorio:** en A-0, hacerlo obligatorio sumó 12,9 puntos de salidas inválidas al primer intento (§8). Ausente o `null` = no habla de ninguno en particular.
2. `product` igual al foco del turno, ausente o `null`: se entrega como hoy. Si el foco del turno vino de una preselección por alias (§4) y `product` la nombra, la preselección queda confirmada (señal 2 de §3).
3. `product` es la clave del principal: se entrega como hoy. El conocimiento del principal viaja siempre en el conocimiento base, así que la respuesta se armó con él. No mueve el foco.
4. `product` es otra clave del índice, distinta del foco del turno y del principal: el bridge **no entrega** esa respuesta ni pide un segundo intento por eso. Sale la **pregunta fija del sistema**, armada con los nombres del índice: «¿Me preguntás por X o por Y?» (X = el foco del turno, Y = el producto del aviso). La respuesta de la persona vuelve a pasar por §3 y §4: si nombra uno, la preselección lo carga y el agente lo confirma.
5. Una clave que no está en el índice invalida la propuesta, como hoy (`unknown_product`), y entra al reintento de salida inválida de siempre.
6. **El segundo intento** (`_MAX_PROPOSAL_ATTEMPTS = 2`, `hermes.py:55`) queda para lo que ya lo usa: la salida inválida y la guarda de precio (`price-guard-v1` §4). Si el último intento trae un `product` del caso 4, sale la pregunta fija. Nunca hay un tercer llamado.

Se registra en la procedencia del turno: `product_hint: {foco, product, fuente_foco, salida}` con `salida` = `entrega` | `pregunta_fija` | `invalida`, sin texto de la persona.

**La rama A, para comparar (no vigente):** `product` distinto del foco no se entregaba, se cambiaba el foco (fuente `agente`) y se repetía el turno con el conocimiento del producto nuevo y la línea «La persona pasó de X a Y…»; si oscilaba, pregunta fija. A-0 no la dejó pasar (§8). Volver a ella es otra versión de este contrato, con otra medición.

## 6. El link

- **El link sale solo del producto en foco** (la guarda de producto ajeno, MP-13, bloqueando desde que se prende el flag, D4). Pedir el link de otro producto **no** cambia el foco (eso era de la rama A).
- `send_payment_link` con `product` distinto del foco del turno (el principal incluido, si no es el foco): el link no sale y sale la pregunta fija de §5.4. Cuando la persona contesta, la preselección carga el producto y el turno siguiente puede mandar su link.
- `send_payment_link` sin ruta para el producto en foco, o un `reply` con `send_payment_link` que nombra otro producto del índice: el link no sale y se deriva con `payment_link_product_unrouted` (el motivo que ya existe, `src/bridge/app.py:9586`).
- Lo demás del link no cambia: la oferta la elige el bridge (la por defecto del producto o la de una campaña vigente de ese producto), nunca el agente.

## 7. Lo que el foco mueve fuera del turno

- **El silencio** (`native_silence.py`, hoy un `product_ref` por barrido): el barrido de un producto atiende las conversaciones cuyo foco es ese producto.
- **La clienta por producto** (MP-14): el cupón y los avisos miran `es_clienta_de(foco)`, no «compró algo». La regla «otro producto: solo si lo pide, sin descuento» sigue viviendo solo en el SOUL común (`SOUL.md:113`) mientras la venta cruzada esté apagada (`product-card-v1` §5, D7).
- **El tablero** (MP-22): cambios de foco por fuente, del historial.

## 8. Medir antes de construir el cambio (A-0) y lo que se decidió

**Lo que se midió.** Antes de MP-12, con el arnés de conversaciones, GLM 5.3 Flash (`z-ai/glm-5.3-flash`, el modelo de producción de Maribel), el SOUL común vigente, el v9 de Maribel y un índice de dos productos, sobre 30 conversaciones reales de Maribel y 20 sintéticas de un producto nuevo escrito con `product-card-v1` (Forever Young), 246 turnos por variante: `a0` con `product` opcional (la que decide) y `a0-oblig` con `product` obligatorio (para la pregunta de §5.1). También latencia y tokens de uno y dos llamados, cuántas respuestas bloquearía la guarda de precio y la preselección por alias calculada en seco.

**El resultado, corrido el 2026-11-14** (encargos E-554 y E-561 del programa etapa-1; resumen en el vault del OS, `coordinacion/etapa-1/multiproducto-a0/resultados/resumen-modelo.txt`, fuera de este repositorio):

| Mide | `a0` (`product` opcional) | `a0-oblig` | Corte |
|---|---|---|---|
| (a) cambios de producto bien marcados con `product` | 77,8 % (35/45): nombrando el producto 28/30, sin nombrarlo 7/15 | 77,8 % | ≥ 90 % ❌ |
| (b) falsos cambios | 1,0 % (2/201) | 1,0 % | ≤ 5 % ✅ |
| (c) salidas inválidas de más, contra hoy | −2,2 puntos | +12,9 puntos | ≤ +2 |
| (d) precio o contenido del producto nuevo dicho sin su conocimiento | 0 % (0/21) | 0 % | ≤ 5 % ✅ |
| (f) la guarda de precio (prototipo) | 0 bloqueos de 432 | 0 | se informa |
| (g) preselección por alias (en seco) | 24 de 24 bien cargados, 0 falsos | | se informa |

Veredicto del arnés: **no pasa**, por (a). Con `product` obligatorio tampoco pasa y empeora (c).

**Lo que se decidió (Dan, 2026-11-14):** rige la **rama B**. MP-13 va sin el cambio por `product`: el foco lo deciden las señales del sistema (§3) y la preselección por los alias declarados (§4, `product-card-v1` §2.1); si no alcanza, la pregunta fija (§5.4). El `product` del agente queda como aviso opcional, nunca obligatorio. Los alias entran al mínimo de `product-card-v1` porque (a) falla justo cuando la persona no nombra el producto (7 de 15). No cambia nada del carril A (release 1): el foco es de la release 3 (MP-12, MP-13).

**Lo que A-0 no midió y queda para MP-12/MP-13:** el agente no se corrió con el bloque preseleccionado cargado (la preselección es en seco), ni con alias declarados (el índice de A-0 era clave y nombre), ni qué contesta la persona a la pregunta fija. Las pruebas de conversación de MP-13 lo miden antes de prender el flag.

## 9. Errores y códigos

`unknown_product` (propuesta inválida, existe), `payment_link_product_unrouted` (derivación, existe), `product_focus_question` (la pregunta fija salió; log y procedencia), `product_focus_changed fuente=<…> conversation=<n>` (log, sin datos personales), `product_hint salida=<entrega|pregunta_fija|invalida> conversation=<n>` (log, sin datos personales).

## 10. Historial

- 0.1.0 (2026-11-13, E-553, #460): estado, señales, preselección por nombre y clave, el cambio por `product` con segundo intento (rama A), el link y la medición A-0 con sus dos ramas. Escrito antes del código: no afirma nada construido.
- 0.2.0 (2026-11-14, E-570): rige la rama B por la decisión de Dan del 2026-11-14 sobre A-0 (§8, con las cifras). §5 pasa a «el aviso del agente»: `product` opcional, nunca obligatorio, no repite el turno ni mueve el foco, y si nombra un producto cuyo conocimiento no estuvo sale la pregunta fija. La señal 2 de §3 pasa a ser la preselección confirmada. §4 usa los alias declarados de `product-card-v1` 0.2.0, con palabras completas y la regla del más largo. §6: pedir el link de otro producto ya no cambia el foco. Escrito antes del código (MP-12, MP-13): no afirma nada construido.
