# ADR-0023: el seguimiento con cupón con manifiesto reusa el lugar `descuento`

- Estado: propuesta, para que Dan la revise con el PR. Los defaults de comportamiento los aceptó el 2026-10-10 sobre la lista recomendada (ver el final de *Decisión*).
- Fecha: 2026-10-10
- Decide: Dan
- Contrato: [conversation-followup-discount-v1.md](../contracts/conversation-followup-discount-v1.md), sección *En un runtime con manifiesto*
- Afina: [ADR-0021](0021-setter-producto-instalable.md), que hace del manifiesto el techo de lo que el runtime puede hacer
- Implementación: migración `20261010000100` y bridge `1.5.0`, sin publicar

## Contexto

El seguimiento con cupón (`CONVERSATION_FOLLOWUP_*`, decisión de Dan del 2026-09-28) le manda una sola vez un 10 % a quien nos escribió, recibió nuestra respuesta y se quedó callado entre 24 y 72 h sin comprar. Nació para Johanna, que corre sin manifiesto, y hasta la 1.4.0 un runtime portable no arrancaba con su flag. El 2026-10-09 Dan decidió que en ATT1 salga «igual que en Johanna», solo a quien contestó (vault: `productos/soporte-infoproductores/decisiones/2026-10-09-el-cupon-de-att1-sale-como-en-johanna-solo-a-quien-contesto.md`).

Habilitarlo con manifiesto chocó con tres cosas del código:

1. **El parser de la plantilla** exigía `{{1}}`, `{{2}}` y `{{3}}`. La plantilla de ATT1, `att1_seguimiento_descuento_01` (aprobada, `es_MX`, `MARKETING`, capturada por la API el 2026-10-10), lleva el código solo en el botón, por decisión de Dan del 2026-10-01.
2. **La identidad.** El barredor reserva con el teléfono del contacto de Chatwoot, que en México es el `wa_id` (`521` + 10 dígitos). En ATT1 el caso puede estar guardado en `52` + 10, porque el primer contacto toma la identidad del formulario, y la reserva y la autorización exigen la identidad exacta. Con la reserva compartida y el `wa_id`, un caso en `52` da `issuance_blocked_identity` y el cupón no sale nunca; un caso en `521` con la intención del formulario en `52` sale con la oferta por defecto y fabrica una segunda intención. El enlace del agente ya había resuelto lo mismo con un par: el resolvedor del entrante y la reserva portable de `20261001000100`.
3. **El manifiesto** no tiene un lugar para esta función. `[flujos]` exige exactamente sus seis flujos, y el lugar `descuento` es el techo de un flujo viejo, el descuento posterior a la respuesta, que nació con el binding v1, no corre junto al agente de Corte B y no manda mensajes: deja una acción diferida.

ATT1 tiene además reglas que Johanna no tiene. La política comercial aprobada pone el 10 % como `later_step`, después de una respuesta a la plantilla inicial (`att1-commercial-006-discount` en `docs/design/att1-commercial-information-approval-v1.md`). Las plantillas que el piloto de ATT1 manda salen de 09 a 21 de Ciudad de México. Y su despachador no manda un saludo que no pasa el filtro de las plantillas.

## Decisión

1. **El flag cuelga del lugar `descuento` del manifiesto.** `[flujos].descuento` es su techo y `[plantillas].descuento` su plantilla. Con manifiesto, `CONVERSATION_FOLLOWUP_TEMPLATE_NAME` y `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` tienen que nombrarla y `CONVERSATION_FOLLOWUP_PRODUCT_NAME` tiene que ser `hotmart.product_name`, o el bridge no arranca. Con el binding v1 en JSON tampoco arranca.
2. **El parser acepta dos formas:** con el cupón también en el texto (`{{1}}`, `{{2}}` y `{{3}}`, la de Johanna) o solo en el botón (`{{1}}` y `{{2}}`, la de ATT1). El cuerpo lleva un parámetro por marcador.
3. **Con manifiesto, la identidad se resuelve como en el entrante** (`resolve_inbound_external_user_id`), y la resuelta va a la reserva y a la autorización. Si la lectura falla, no se reserva nada.
4. **La reserva es una RPC portable derivada:** `claim_portable_conversation_followup_v1`, creada con `pg_get_functiondef` y `replace` sobre la definición vigente de `claim_conversation_followup_v1`, el método de `20261001000100`. Tiene tres cambios contados: el nombre, el enlace emitido por `reserve_portable_checkout_issuance_v2` y la barrera del punto 5. La compartida no se toca, y el bridge sin manifiesto la sigue llamando con los mismos argumentos.
5. **El cupón sale solo en conversaciones adoptadas.** La RPC portable devuelve `blocked_not_template_reply` si la conversación no tiene el evento de adopción (`inbound_adopted_template_conversation`), que la admisión portable deja al adoptar la conversación de una plantilla nuestra del piloto cuando la persona responde. Es la política `later_step`.
6. **El horario es obligatorio con manifiesto:** `CONVERSATION_FOLLOWUP_SEND_HOURS=HH-HH`, en la zona del manifiesto, con `09-21` para ATT1. `00-24` es a cualquier hora y hay que escribirlo. Sin manifiesto no se admite: no hay zona en la que leerlo.
7. **Con manifiesto, un nombre que no pasa `template_greeting_name_is_safe` no se manda.** Se reintenta en cada barrido y se cuenta como `greeting_name_refused`.
8. **Antes de abrir se prueba con un solo teléfono** (`CONVERSATION_FOLLOWUP_ONLY_PHONE`): solo ese teléfono recibe, y el resto de las candidatas se cuenta como `held_only_phone`. Ese número es una cota superior del último barrido, candidatas según Chatwoot antes de las barreras de la base, y nunca dice a cuántas personas les habría salido. `/ready` informa a quién le puede escribir (`conversation_followup_audience`) y cuánto silencio exige (`conversation_followup_min_age_seconds`), sin mostrar ningún número. Menos de 24 h de silencio es solo para la prueba: con manifiesto y sin el teléfono de prueba, el bridge no arranca.

Los puntos 5 a 8 y el tratamiento de las preguntas por el descuento (el agente las deriva y el conocimiento no cambia) son D2 a D6 del relevamiento. Se tomaron con el default recomendado: Dan aceptó la lista de cinco con un «Van» el 2026-10-10 (vault: `productos/soporte-infoproductores/decisiones/2026-10-10-el-cupon-de-att1-va-con-los-defaults-recomendados.md`). Como la aceptó en bloque, quedan marcados para que los revise con el PR. El punto 1 es un default técnico del mismo relevamiento que Dan no objetó (D1). Los puntos 2 a 4 son la arquitectura que el relevamiento recomendó para que el cupón salga con la plantilla aprobada, la identidad del caso y la oferta del formulario.

## Consecuencias

- **Johanna no cambia.** Sin manifiesto, el barredor llama a la misma RPC con los mismos 14 argumentos, a cualquier hora y a todo el inbox, y su `/ready` trae las dos claves de siempre. Su próximo despliegue desde `main` se lleva el parser nuevo, que con su plantilla de tres marcadores arma el mismo cuerpo, y la lista de resultados con `blocked_not_template_reply`, que su RPC no devuelve. En su base la migración avisa y no crea nada, y la huella del inventario da `fingerprint_absent`.
- **Cuatro desvíos de «igual que en Johanna»** en una instancia con manifiesto: no sale a quien escribió por su cuenta, no sale de noche, no sale con un nombre inseguro y se prueba con un teléfono antes de abrir. El horario y el modo de prueba son valores del despliegue: `00-24` es el horario de Johanna, y sin `CONVERSATION_FOLLOWUP_ONLY_PHONE` el alcance es el del entrante. Con los remitentes por scope, como en ATT1, es todo el inbox; con `ALLOWED_WHATSAPP_JID` sin scope es solo ese número, y `/ready` lo dice (`allowed_jid`, no `inbox`). La barrera y el filtro del nombre van atados al manifiesto, y cambiarlos pide código.
- **Volver atrás es sacar el flag y redesplegar.** El manifiesto no suma claves, así que bajar a una imagen anterior solo pide sacar el flag antes. La migración queda: solo agrega una función.
- **`validate` no distingue cuál de los dos flags habilita `descuento`.** Prender el lugar para el seguimiento levanta también el techo del descuento posterior a la respuesta, que sigue frenado por su propia guarda: no corre con el agente de Corte B.
- **Una RPC más para mantener a la par.** Un cambio futuro de `claim_conversation_followup_v1` tiene que llegar también a la portable, como pasa con la reserva portable de `20261001000100` respecto de la compartida. Si al correr la migración los tres textos no están exactamente una vez, falla con `55000` en vez de derivar a ciegas.
- **El seguimiento no pasa por el piloto:** ni por su pausa, ni por sus topes, ni por el tope de mensajes proactivos por persona de la 1.4.0, ni por `META_FINAL_EFFECT_ENABLED`. Se frena sacando su flag. No tiene tope diario: uno por conversación y, por defecto, diez por barrido.
- **Fuera del horario el barredor publica `healthy` sin leer el catálogo.** Una plantilla que Meta pausó recién se ve en el primer barrido del horario.

## Alternativas descartadas

- **Un flujo y un lugar nuevos en el manifiesto (`seguimiento`).** Como clave obligatoria, ningún manifiesto carga hasta sumarla. Como clave opcional no rompe nada, pero una imagen anterior no carga un manifiesto con una clave que no conoce, así que volver atrás pide sacarla antes de bajar. El lugar `descuento` ya nombra la plantilla de descuento de la instancia, y su flujo viejo no manda nada.
- **Arreglar solo el parser y la portabilidad, con la reserva compartida.** Con el caso en `52` el cupón no saldría nunca, y con el caso en `521` saldría con la oferta por defecto y una intención fabricada.
- **Resolver la identidad adentro del SQL.** Funciona, pero suma reemplazos anclados a la función. El resolvedor del bridge con la reserva portable es el camino que ya usa el enlace del agente.
- **Agregarle un parámetro a `claim_conversation_followup_v1`.** Le cambia la firma a la función que corre Johanna.
- **Una plantilla nueva con el código en `{{3}}`.** Contradice la decisión de Dan del 2026-10-01 y depende de una aprobación nueva de Meta.
- **El cupón como texto libre dentro de la ventana de 24 h.** Lo descartó Dan el 2026-09-28: lleva el enlace largo y es código nuevo.
- **El flujo viejo, `CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED`.** No corre con el agente de Corte B, no manda nada y exige políticas de descuento que ninguna instancia sembró.
- **Mandar el cupón por el piloto** (scope, topes y despachador). Es otro diseño, bastante más largo.
- **El 10 % en la primera plantilla del carrito.** Contradice la política `later_step` y pide otra plantilla.
- **Un modo sombra, sin envío.** El modo de un solo teléfono ya cuenta las candidatas, y además permite la prueba de punta a punta.
