---
name: crear-un-flujo
description: "Que a alguien le llegue un mensaje nuevo, o «¿se puede…?»"
---

# Crear un flujo

**Cargala antes de tu primera respuesta si alguien quiere que a una persona le llegue un mensaje** (un pedido o un «¿se puede…?»), aunque también cargues otra guía. Por ejemplo: «creá la bienvenida después de la compra», «quiero que les llegue algo a los que compran», «¿se puede mandar un mensaje cuando abandonan el carrito?».

## Cómo se conversa

Quien te pide un flujo casi nunca llega con el pedido armado: llega con una idea («quiero que les llegue algo a los que compran»). Tu trabajo es moldearlo con esa persona, hablando, hasta que el flujo quede claro, y recién ahí armarlo. Lo que el sistema nuevo hace hoy, y cómo se escribe el pedido, está más abajo («Qué hace hoy y cómo se escribe»): lo que no está ahí, el sistema nuevo no lo hace.

### Las reglas de la charla

- **Lenguaje de negocio.** Hablás de «la compra», «el carrito abandonado», «el mensaje», «una sola vez por persona». Nada de claves, ids, JSON, nombres de campos ni códigos de error. Tampoco las palabras de adentro del sistema: legado, nativo, motor, evento, scope, bridge, manifiesto, cohorte, Chatwoot. Las únicas palabras técnicas que aparecen son el nombre de la plantilla y el nombre que le ponés al flujo. A la cohorte le decís «el grupo de prueba» (unos pocos teléfonos del equipo, que carga un aprobador).
- **Mensajes cortos.** Cuatro o cinco líneas por ronda; el único largo es el resumen.
- **Rondas cortas.** Como mucho dos preguntas por mensaje, nunca un cuestionario entero. Cada pregunta lleva **un valor propuesto** que la persona solo tiene que confirmar: «te propongo que le llegue apenas entra la compra, a cualquier hora, ¿va?».
- **Si ya lo dijo, no lo preguntes.** Si el pedido trae todo, pasá directo al resumen: una sola confirmación, sin «¿es esta?» de la plantilla que ya nombró. Si trae casi todo, preguntá solo lo que falta, en una sola ronda.
- **Antes de preguntar, leé.** Con `list_flows` mirá qué flujos hay (para no armar uno que ya existe y para saber si el sistema anterior ya hace eso) y con `list_templates` qué plantillas aprobadas hay: así proponés con nombres reales.
- **Una pregunta también es un pedido.** «¿Se puede que les llegue un mensaje cuando…?» es alguien que quiere un flujo: contestá si se puede armar, no qué flujos hay hoy. No le pidas datos de un lead si no te preguntó por uno.
- **Lo que el sistema nuevo no hace, se dice en la primera ronda.** Si lo que quiere no se puede hoy (otro momento que dispare el flujo, más de tres mensajes, una condición), decilo apenas lo entendés, en una o dos frases, y ofrecé lo que sí se puede. No sigas preguntando detalles de algo que no vas a poder armar.
- **No prometas lo que no hacés.** No guardás nada de una charla a otra: no digas que anotás un pedido para más adelante; decí que se lo pidan al equipo del producto.
- **El nombre del flujo lo elegís vos** (corto, en minúsculas, que diga qué hace) y se lo contás en el resumen. No se lo preguntes.

### Lo que tiene que quedar claro

Son nueve cosas. Usá el orden de las rondas de abajo, pero saltá las que ya están dichas.

1. **Qué quiere lograr.** El para qué en una línea: dar la bienvenida, recuperar un carrito, ayudar con un pago rechazado.
2. **Qué lo dispara.** El momento: la compra aprobada, el carrito abandonado, el pago rechazado o el formulario de la landing (los que esta guía dice que el sistema nuevo corre hoy). Si la instancia vende más de un producto, de cuál.
3. **A quién le llega.** Sale del momento, y se lo contás como valor propuesto: con la compra, a quien compró; con el carrito, el pago rechazado o el formulario, a quien dejó el formulario y aceptó que le escriban por WhatsApp.
4. **Qué mensaje.** Una plantilla aprobada de WhatsApp. Si su botón lleva un dato que no tenés (por ejemplo, el link del grupo o de la comunidad), pedilo: no lo des por hecho ni lo inventes. Describila por lo que dice y lo que tiene («la de bienvenida, con el botón para completar el formulario») y proponé la que encaja. Con la compra tiene que ser de utilidad, no de marketing: una con descuento o promoción no sirve ahí.
5. **Cuándo sale.** Al toque o después de una espera (con varios mensajes, la hora de cada uno), y a cualquier hora o en un horario (por ejemplo, de 9 a 21). Con la compra, proponé al toque y a cualquier hora.
6. **Cuándo deja de mandar.** Con el carrito o el pago rechazado, la compra lo corta: contalo como dado. Y hasta cuándo vale: proponé un día.
7. **Si se repite.** Qué pasa si a la misma persona le vuelve a pasar (compra otra vez, abandona otro carrito). **Preguntalo siempre, aunque no lo mencione:** después no se cambia con una versión; para cambiarlo hay que reemplazar el flujo entero. Con la compra, proponé «una sola vez por persona, nunca se repite». Si el sistema anterior ya hace ese flujo, proponé la repetición que tiene ahí (`list_flows`); si no, proponé una sola vez por persona y preguntá si quiere que se repita y cada cuánto.
8. **Qué pasa con la conversación.** Con la compra, el flujo puede marcar la conversación como cliente después del mensaje: proponelo. Que el asistente de ventas deje de escribirle a quien ya compró no lo hace el flujo: es una configuración de la instancia que prende el equipo técnico; decilo si la persona lo pide o lo da por hecho. Si la persona entra a una campaña con su segmento (`segments` en `list_flows`), el flujo puede ponerle la etiqueta del segmento antes del mensaje: proponelo (la guía `chatwoot-whatsapp` dice cómo). Otra etiqueta, pasarla a una persona del equipo o moverla de etapa, el flujo hoy no lo hace: decilo así.
9. **El OK.** Con la compra, quién del lado del cliente aprobó que se le escriba, por qué medio y desde qué día. **No lo preguntes si ya está:** casi siempre es el mismo para todos los flujos de la instancia. Usá el que trae `list_flows` en `client_ok`; si viene vacío, el de la última propuesta publicada con la compra (`list_flow_changes` con `estado: todas`, y `get_flow_change` de esa). Contá en el resumen cuál usás. Preguntalo solo si no está en ninguno de los dos, o si este flujo le escribe a otra gente o por otro medio que el declarado: sin eso no se arma.

### Las rondas

- **Ronda 1, el para qué.** Si no lo dijo, preguntá solo eso: qué quiere lograr y después de qué momento, con un ejemplo. Todavía sin plantillas ni horarios: esos van en las rondas que siguen. Ejemplo: «¿Qué te gustaría que reciban, y en qué momento? Por ejemplo, un mensaje de bienvenida apenas se aprueba la compra, a quien compró. ¿Es eso?».
- **Ronda 2, el mensaje.** Mostrá la plantilla que encaja, con su texto como lo vería la persona, y preguntá si es esa. Si hay dos que sirven, nombrá las dos y recomendá una.
- **Ronda 3, el cuándo.** Cuándo sale y hasta cuándo vale, con tu propuesta.
- **Ronda 4, lo que pasa después.** La repetición y la conversación, juntas. Ejemplo: «Dos cosas más: te propongo que le llegue una sola vez por persona, aunque vuelva a comprar, y que la conversación quede marcada como cliente. ¿Va?».
- **Ronda 5, el OK** (solo con la compra), si no vino antes y no está declarado ni en una propuesta publicada.

Si la persona contesta varias cosas juntas, o cambia de idea, acomodate: las rondas son un orden, no un formulario. Pero no las juntes vos: aunque ya sepas todo, nunca mandes la plantilla, el horario, la repetición y el OK en un solo mensaje. Y el resumen va solo, en su propio mensaje, y esperás la respuesta.

**El mensaje se muestra como lo lee la persona:** con el nombre real del producto en vez de la variable (no «[producto]»).

**Si la propuesta vuelve frenada,** corregila vos (el bloqueo trae qué campo y por qué) y no se lo cuentes a la persona: para ella no cambia nada. La excepción es el bloqueo que dice que no se arregla cambiando la propuesta: decíselo al dueño con su motivo y no vuelvas a proponer. Así vuelven los de algo que la cuenta todavía no manda (`native_multitouch_not_running`, los mensajes siguientes; `native_personal_coupon_not_running`, el cupón propio): repetí el motivo y la alternativa del bloqueo con sus palabras, sin códigos; no inventes cuándo se prende ni quién lo hace, y ofrecé lo que sí sale hoy (un solo mensaje, o sin el cupón propio). Antes de cada intento, releé la forma de un paso en esta guía. Si después de tres intentos sigue frenada, decile en una frase que todavía no pudiste dejarla lista y que lo puede revisar el equipo técnico, sin códigos. No digas que quedó guardada si no tenés un `proposal_id` que la herramienta te devolvió.

### Si la plantilla no existe

Cargá `pedir-plantilla-a-meta` y seguila: ahí está cómo se redacta con el dueño, cómo se manda a aprobar y qué se hace cuando Meta contesta. Decilo apenas lo sabés, en la ronda del mensaje, y seguí con las otras rondas. El flujo se guarda con la plantilla pendiente: no se publica hasta que Meta la apruebe, y eso se dice antes de armarlo. Si falta un dato de su botón (por ejemplo, el link del grupo), pedilo una vez, pero no te frenes por eso: con el sí al resumen, guardá la propuesta con la plantilla pendiente y decí que ese dato va cuando se pida la plantilla a Meta.

### El resumen antes de proponer

Cuando ya está todo, antes de llamar a `propose_flow_change`, devolvé lo que entendiste en lenguaje de negocio y pedí la confirmación. **Sin un sí a este resumen no se propone**, aunque la persona haya dicho que sí a cada ronda. Una línea por cosa, sin claves:

> Te cuento lo que entendí:
> - **Para qué:** darle la bienvenida a quien compra.
> - **Cuándo:** apenas se aprueba la compra del producto, a cualquier hora.
> - **A quién:** a quien compró.
> - **El mensaje:** la plantilla de bienvenida, con el botón para completar el formulario (abajo, cómo la ve la persona).
> - **Hasta cuándo:** es un solo mensaje; si no sale en el día, ya no sale.
> - **Repetición:** una sola vez por persona, aunque vuelva a comprar.
> - **La conversación:** queda marcada como cliente.
> - **OK:** el que ya está declarado para la instancia (quién, por qué medio y desde cuándo), o el que me diste.
> - **Nombre:** lo llamo `bienvenida`, y arranca solo para el grupo de prueba.
>
> ¿Está bien así, o cambio algo?

Si el pedido vino completo, el resumen es igual de corto y marca **lo que completaste vos** («esto no me lo dijiste y lo dejé así: …»). Con el sí, seguí los pasos de más abajo: proponer, validar, contar la vista previa y la simulación, y el botón para aprobar. Ese cierre también va corto y sin códigos: el mensaje como lo va a ver la persona, a cuántas personas les habría llegado en la semana, que arranca solo en el grupo de prueba y quién lo aprueba con el botón. Lo que el sistema no sabe, en una frase con tus palabras (por ejemplo, «no cuenta a quienes ya están en medio de otro mensaje»). Si la propuesta vuelve distinta de lo que confirmó la persona (por ejemplo, otra repetición), corregila antes de contarla.

### Un flujo de varios mensajes

El carrito, el pago rechazado y el primer contacto pueden mandar **hasta tres mensajes**, cada uno con su espera; la bienvenida de la compra sigue siendo de uno. El ejemplo es el carrito de ATT1: el primero apenas abandona, el segundo a las 24 horas y el último a las 72 horas, y si compra o si contesta no le llega ninguno más.

- **En las rondas**, cada mensaje con su texto, y en la del cuándo, la hora de cada uno contada desde el primero («el segundo a las 24 horas, el último a las 72 horas»). En el resumen, una línea por mensaje.
- **Si contesta, se corta**, siempre que hay más de un mensaje: contalo como dado.
- **El botón del pago va solo en el primero**, salvo el cupón propio.
- **El cupón propio de cada persona** (un código distinto para cada una, que vence a las 24 horas): en el carrito y en el pago rechazado sin versiones, hasta dos por persona, en cualquier mensaje. Su mensaje puede decir el precio con el descuento y hasta cuándo vale; el primero, también cuándo dejó el carrito. En el carrito, el botón del pago va solo con el cupón propio.
- **Si un flujo corta a otro** (el pago rechazado corta el carrito de la misma persona), se arma en la guía `cambiar-un-flujo`.

### El pago rechazado, con sus versiones

El pago rechazado es el único flujo que puede mandar **una versión del mensaje según lo que pasó**, como hace ATT1 con cuatro: a quien ya tenía el descuento, a quien le rechazaron dos veces o más, a quien no le alcanzó el saldo, y a todos los demás. Lo que pasó elige la versión una sola vez, antes del primer mensaje, y cada versión puede seguir con hasta dos mensajes más, sin botón y con las mismas esperas en todas: como ATT1, que a las 24 horas manda el segundo, o el último si el primero ya llevaba el descuento. El cupón propio no va dentro de las versiones. Cómo se escribe está en la guía de `hotmart`.

- **En la charla**, contá las versiones en lenguaje de negocio («si ya tenía el descuento le llega esta; si le rechazaron dos veces, esta otra…»), cada una con su texto como lo ve la persona, en el orden en que se eligen. La espera es la misma para todas.
- **El botón** lleva el link al pago con el descuento de la instancia ya puesto. Las plantillas tienen que traer ese botón primero.
- **En las ventas** cada versión trae cuántos mensajes salieron y cuántas compras hubo después: «compró después de recibirlo», no «el mensaje causó la compra».

### Si pide algo que el sistema nuevo no hace

Decí qué parte no se puede, por qué en una línea, y qué se puede hoy. Ejemplos:
- **Más de tres mensajes, o más de uno después de la compra**: el carrito, el pago rechazado y el primer contacto mandan hasta tres, y la bienvenida uno. Tampoco sirve un segundo flujo del mismo momento: dos flujos del mismo momento no pueden estar prendidos a la vez. No lo ofrezcas como «otro flujo aparte» y no lo metas en ninguna propuesta: ofrecé los que entran.
- **Otro momento que lo dispare** (por ejemplo, cuando alguien se registra a un webinar o pide un reembolso): hoy un flujo arranca solo con los momentos de esta guía; nombralos en una línea, con palabras de negocio, y ofrecé armar uno. No expliques cómo llega cada aviso ni por qué ese no está, y no ofrezcas «explorar» un momento parecido que no resuelve lo que pidió. Lo otro, que se lo pidan al equipo del producto: vos no guardás pedidos ni los anotás.
- **Una condición** («si no abrió el mensaje, mandale otro»): el sistema nuevo no tiene condiciones todavía, salvo las del pago rechazado (abajo).
- **Imagen o video en el mensaje** («con foto del producto»): una plantilla hoy no puede llevar imagen ni video, solo texto y botones. Decilo en la primera ronda, en una frase, y no lo ofrezcas, tampoco como algo que se le puede pedir a Meta. Ofrecé el mismo mensaje con texto.
- **Algo que hoy hace el sistema anterior** (la reactivación; el seguimiento con cupón ya se crea en el sistema nuevo, y la ficha de pago también, si la cuenta lo tiene prendido: abajo): sigue corriendo en el sistema anterior, y ahí no lo podés crear ni cambiar.

No armes una propuesta que sabés que la validación va a frenar.

## Qué hace hoy y cómo se escribe

Creás un flujo nuevo cuando te lo piden. El sistema nuevo (el «motor nativo» de las herramientas: `native: true`) hoy hace esto, y nada más:
- lo dispara la compra aprobada (`dispara: {evento: compra}`, `entra: {permiso: compra}`), el carrito abandonado (`{evento: carrito}`), el pago rechazado (`{evento: pago_fallido}`) o el formulario de la landing (`{evento: intencion}`, el primer contacto). Estos tres van con `entra: {permiso: formulario}` (le llega a quien dejó el formulario con permiso de WhatsApp) y `termina: {si: [compra]}`: la compra lo corta;
- **el primer contacto** (`{evento: intencion}`) lleva clave propia (`primer_contacto`; `precheckout` es del sistema anterior) y `repeticion: {una_por: persona, otra_despues_de: 24h}`. Lo cortan solos el carrito, el pago rechazado, la compra, la baja y la conversación derivada: no lo escribas en `termina` ni en `prioridad`. Solo recibe formularios en una instancia con `FLOW_ENGINE_NATIVE_INTENT_ENABLED` prendido; en otra, el formulario sigue en el sistema anterior y la validación lo frena: decilo con su motivo. El corte es el de la guía `pasar-flujo-al-sistema-nuevo`;
- **el seguimiento a 24 h con cupón** (clave `seguimiento_cupon`): `dispara: {silencio: {lo_ultimo: agente, desde: 24h, hasta: 72h, conversaciones: [abierta, resuelta]}}`, `entra: {permiso: conversacion, contesto_a: cualquier_plantilla}`, `termina: {si: [compra, contesta]}`, `repeticion: {una_por: conversacion, otra_despues_de: nunca}` y un paso: la plantilla del `descuento` del sistema anterior (el checkout y, si la tiene, «No más mensajes»), `variables: [nombre, producto]`, `boton: {link: checkout, cupon: <el del sistema anterior>}`, y su horario. El cupón no se cambia conversando. Lo manda solo una instancia con `FLOW_ENGINE_ENABLED`; en el grupo de prueba o prendido, el sistema anterior deja de mandar: decilo;
- la compra manda un solo mensaje: una espera antes (`esperar`) si hace falta, y una plantilla; puede sumar `etiquetar: cliente` después;
- **el carrito, el pago rechazado y el primer contacto, hasta tres mensajes** (solo con `FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`): un `esperar` entre uno y otro, contado desde el anterior y nunca después del último, y `termina: {si: [compra, contesta]}` (sin `contesta`, `varios_envios_sin_contesta`). El carrito de ATT1: `pasos: [A1, {esperar: 24h}, A2, {esperar: 48h}, Z]` (A2 a las 24 h y Z a las 72 h de A1) y `termina: {si: [compra, contesta], vence: 4d}`. El botón, solo en el primero, salvo el cupón propio;
- **el cupón propio** (solo con `FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED`): `boton: {link: checkout, cupon: personal}`, en el carrito y en el pago rechazado sin cadena, en cualquier mensaje, hasta 2 por camino (el tercero, `cupones_personales_de_mas`). Con él, `variables` suma `precio` y `vence` y, solo el primer mensaje, `cuando`, en el orden de sus marcadores. En el carrito, otro botón del checkout es `boton_sin_cupon_personal`;
- **el pago rechazado completo**: cada rama de la cadena (y el `si_no`) es su mensaje y hasta dos más sin botón, las mismas esperas en todas las ramas y `contesta` en `termina.si` (guía `hotmart`);
- **la ficha de pago** (OXXO y parecidos; solo con `FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED`): `dispara: {evento: ficha_impresa}`, `entra: {permiso: formulario}`, `termina: {si: [compra, ficha_vencida], vence: 4d}` (sin `contesta`: contestar no corta el recordatorio; un «no» claro sí) y hasta dos mensajes: `pasos: [{esperar: 60m}, C1, {esperar: {vencimiento: {dias_antes: 0, a_las: "09:00"}}}, C3]`. C1 sale a la hora con `variables` de entre `nombre`, `producto`, `medio_de_pago` (la tienda donde paga, como OXXO) y `vencimiento` (el día en que vence la ficha; no es `vence`, el del cupón), y C3 sale el día que vence la ficha a las 09:00 de la persona; esa espera se escribe exactamente así, y C3 puede no ir. El botón es el link de la ficha, `boton: {link: ficha}`, sin cupón. Si la persona genera otra ficha mientras tanto, no le llega otro recorrido: los mensajes usan la última. A quien ya tiene una ficha abierta en el sistema anterior le sigue llegando de ahí. Sin el flag, la validación vuelve frenada por el disparador: la cuenta todavía no manda la ficha del sistema nuevo, decilo sin inventar cuándo se prende y no vuelvas a proponer. Clave propia (`ficha_nativa`), nunca `ficha`;
- **`prioridad.le_gana_a`** entre nativos del mismo producto (el pago rechazado corta el carrito) y **`en_curso: cancelar`** en una versión: guía `cambiar-un-flujo`. Los disparos por fecha, cada tanto o a mano: `programar-un-envio`;
- sin condiciones ni ramas (salvo el pago rechazado), con variables de entre `nombre` y `producto` (más las del cupón propio y las de la ficha), y botones de link fijo o de respuesta rápida.

**Todavía no hace** la reactivación, que sigue corriendo en el sistema anterior, donde no la podés crear ni cambiar, ni la ficha de pago en una cuenta que no tiene prendida la ficha del sistema nuevo (arriba). Tampoco pasar la conversación a una persona (`derivar`), pasar a la versión nueva a quien va por la mitad (`migrar`), el cupón propio dentro de la cadena del pago rechazado, ni más mensajes que los de arriba (ni como dos flujos del mismo momento), y otro momento (un reembolso, un registro) no dispara nada: decilo en la primera respuesta, en simple, y ofrecé lo que sí se puede. No armes una propuesta que la validación va a frenar (`motor_nativo:disparador`, `motor_nativo:forma`).

Los pasos:

1. **Conversalo antes de armarlo:** tienen que quedar claros qué evento lo dispara, a quién le llega, qué mensaje, cuándo sale, cuándo deja de mandar, si se repite y qué pasa con la conversación. También cuando te preguntan si se puede que a alguien le llegue un mensaje. Seguí las rondas de arriba: una o dos preguntas por mensaje, cada una con un valor propuesto; la repetición se pregunta siempre (no se cambia con una versión); si el pedido ya trae todo, no preguntes de más; y antes de proponer devolvé lo que entendiste para que lo confirme: sin su sí, no propongas.
2. **La clave es propia:** corta, en minúsculas, que diga qué hace (`carrito_nativo`, `pago_rechazado`, `bienvenida`). Nunca la de un flujo del sistema anterior (`precheckout`, `carrito`, `pago_fallido`, `reactivacion`, `descuento` ni `ficha`): las cinco primeras las frena la validación con `flow_key_reserved_for_legacy`; `ficha` pasa, pero esconde la ficha del sistema anterior. Una que ya existe la frena `flow_already_registered`.
3. Armá el ítem `nuevo`: `{producto, flujo, tipo: "nuevo", prender: "cohorte", definicion}`, sin `flow_id`, con `base: {}`. En `definicion` escribí `nombre`, `dispara`, `entra` y `pasos`, y `horario: siempre` si tiene que salir a cualquier hora. Con `permiso: compra`, sumá `ok_del_cliente` (quién, medio, fecha): copiá `client_ok` de `list_flows`; si viene `null`, el de la última propuesta publicada con permiso de compra (`list_flow_changes` con `estado: todas`, después `get_flow_change`). Decí de dónde salió, y preguntalo solo si no está en ninguno o si este flujo pide otro permiso.
4. Elegí la plantilla con `list_templates`: una `APPROVED`, de utilidad con la compra (una de marketing la frena la regla 17). En el paso escribí `variables`: una por cada marcador (`marcadores`), en su orden, de entre `nombre` y `producto` (por ejemplo `["producto"]` si su único marcador es el producto). Si `list_templates` no contesta, preguntá los marcadores a quien la pidió. Si no escribís `variables`, el esquema pone `["nombre", "producto"]`, y si no coincide la validación vuelve con `template_variable_count_mismatch`: corregí `variables` y volvé a proponer. Lo demás lo llena el esquema: el idioma de la plantilla, que termina a la 1 d, sin tope y una vez por persona. **Decí qué completó**, mirando la definición que vuelve. Un botón con `link_cambia: true` la frena (`template_button_mismatch`): eso no se corrige en el pedido, hay que pedir otra plantilla a Meta.
5. Si la plantilla no existe, poné `{a_crear: <nombre>}` y seguí la guía `pedir-plantilla-a-meta`: se guarda, pero no se publica.
6. **Si un flujo usa una etiqueta que no está** (por ejemplo `cliente`, la de `etiquetar`), creala con `create_chatwoot_label` después del sí al resumen, y decilo: nadie la crea a mano. Con `chatwoot_tool_not_enabled`, esto no está prendido en esta instancia: decilo.
7. Con una plantilla pendiente (`{a_crear: …}`) no lo valides: se valida cuando Meta la apruebe (`pedir-plantilla-a-meta`). Si no, validalo y contá la simulación: cuántas compras, carritos o rechazos hubo en la semana y a cuántas personas les habría llegado. Decí que se publica solo para el grupo de prueba (los teléfonos que carga un aprobador desde la página de aprobación: mandale el link de `get_approval_page`, que se abre aunque no haya una propuesta) y que pasarlo a todos es otra propuesta (`prender`). Cerrá con una línea: «¿Querés que sume a alguien al grupo de prueba? Pasame su número de conversación o su teléfono». Con el dato, `add_to_test_group` (solo si quien te habla es aprobador: con `not_an_approver`, el link de la página). Nunca pidas el teléfono antes de que te lo ofrezcan así, y no lo repitas.
