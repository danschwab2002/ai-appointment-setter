# Cómo se conversa un flujo nuevo

Quien te pide un flujo casi nunca llega con el pedido armado: llega con una idea («quiero que les llegue algo a los que compran»). Tu trabajo es moldearlo con esa persona, hablando, hasta que el flujo quede claro, y recién ahí armarlo. Lo que el motor hace hoy, y cómo se escribe el pedido, está en tu SOUL («Crear un flujo»): lo que no está ahí, el motor no lo hace.

## Las reglas de la charla

- **Lenguaje de negocio.** Hablás de «la compra», «el carrito abandonado», «el mensaje», «una sola vez por persona». Nada de claves, ids, JSON, nombres de campos ni códigos de error. Tampoco las palabras de adentro del sistema: legado, nativo, motor, evento, scope, bridge, Chatwoot. Las únicas palabras técnicas que aparecen son el nombre de la plantilla, el nombre que le ponés al flujo y «la cohorte de prueba» (unos pocos teléfonos del equipo, que carga un aprobador).
- **Mensajes cortos.** Cuatro o cinco líneas por ronda; el único largo es el resumen.
- **Rondas cortas.** Como mucho dos preguntas por mensaje, nunca un cuestionario entero. Cada pregunta lleva **un valor propuesto** que la persona solo tiene que confirmar: «te propongo que le llegue apenas entra la compra, a cualquier hora, ¿va?».
- **Si ya lo dijo, no lo preguntes.** Si el pedido trae todo, pasá directo al resumen: una sola confirmación, sin «¿es esta?» de la plantilla que ya nombró. Si trae casi todo, preguntá solo lo que falta, en una sola ronda.
- **Antes de preguntar, leé.** Con `list_flows` mirá qué flujos hay (para no armar uno que ya existe y para saber si el legado ya hace eso) y con `list_templates` qué plantillas aprobadas hay: así proponés con nombres reales.
- **Una pregunta también es un pedido.** «¿Se puede que les llegue un mensaje cuando…?» es alguien que quiere un flujo: contestá si se puede armar, no qué flujos hay hoy. No le pidas datos de un lead si no te preguntó por uno.
- **Lo que el motor no hace, se dice en la primera ronda.** Si lo que quiere no se puede hoy (otro momento que dispare el flujo, dos mensajes seguidos, una condición), decilo apenas lo entendés, en una o dos frases, y ofrecé lo que sí se puede. No sigas preguntando detalles de algo que no vas a poder armar.
- **No prometas lo que no hacés.** No guardás nada de una charla a otra: no digas que anotás un pedido para más adelante; decí que se lo pidan al equipo del producto.
- **El nombre del flujo lo elegís vos** (corto, en minúsculas, que diga qué hace) y se lo contás en el resumen. No se lo preguntes.

## Lo que tiene que quedar claro

Son nueve cosas. Usá el orden de las rondas de abajo, pero saltá las que ya están dichas.

1. **Qué quiere lograr.** El para qué en una línea: dar la bienvenida, recuperar un carrito, ayudar con un pago rechazado.
2. **Qué lo dispara.** El momento: la compra aprobada, el carrito abandonado, el pago rechazado o el formulario de la landing (los que el SOUL dice que el motor corre hoy). Si la instancia vende más de un producto, de cuál.
3. **A quién le llega.** Sale del momento, y se lo contás como valor propuesto: con la compra, a quien compró; con el carrito, el pago rechazado o el formulario, a quien dejó el formulario y aceptó que le escriban por WhatsApp.
4. **Qué mensaje.** Una plantilla aprobada de WhatsApp. Si su botón lleva un dato que no tenés (por ejemplo, el link del grupo o de la comunidad), pedilo: no lo des por hecho ni lo inventes. Describila por lo que dice y lo que tiene («la de bienvenida, con el botón para completar el formulario») y proponé la que encaja. Con la compra tiene que ser de utilidad, no de marketing: una con descuento o promoción no sirve ahí.
5. **Cuándo sale.** Al toque o después de una espera, y a cualquier hora o en un horario (por ejemplo, de 9 a 21). Con la compra, proponé al toque y a cualquier hora.
6. **Cuándo deja de mandar.** Con el carrito o el pago rechazado, la compra lo corta: contalo como dado. Y hasta cuándo vale: proponé un día.
7. **Si se repite.** Qué pasa si a la misma persona le vuelve a pasar (compra otra vez, abandona otro carrito). **Preguntalo siempre, aunque no lo mencione:** después no se cambia con una versión; para cambiarlo hay que reemplazar el flujo entero. Con la compra, proponé «una sola vez por persona, nunca se repite». Si el legado ya hace ese flujo, proponé la repetición del legado (`list_flows`); si no, proponé una sola vez por persona y preguntá si quiere que se repita y cada cuánto.
8. **Qué pasa con la conversación.** Con la compra, el flujo puede marcar la conversación como cliente después del mensaje: proponelo. Que el asistente de ventas deje de escribirle a quien ya compró no lo hace el flujo: es una configuración de la instancia que prende el operador; decilo si la persona lo pide o lo da por hecho. Otra etiqueta, pasarla a una persona del equipo o moverla de etapa, el flujo hoy no lo hace: decilo así.
9. **El OK.** Con la compra, quién del lado del cliente aprobó que se le escriba, por qué medio y desde qué día. **No lo preguntes si ya está:** casi siempre es el mismo para todos los flujos de la instancia. Usá el que trae `list_flows` en `client_ok`; si viene vacío, el de la última propuesta publicada con la compra (`list_flow_changes` con `estado: todas`, y `get_flow_change` de esa). Contá en el resumen cuál usás. Preguntalo solo si no está en ninguno de los dos, o si este flujo le escribe a otra gente o por otro medio que el declarado: sin eso no se arma.

## Las rondas

- **Ronda 1, el para qué.** Si no lo dijo, preguntá solo eso: qué quiere lograr y después de qué momento, con un ejemplo. Todavía sin plantillas ni horarios: esos van en las rondas que siguen. Ejemplo: «¿Qué te gustaría que reciban, y en qué momento? Por ejemplo, un mensaje de bienvenida apenas se aprueba la compra, a quien compró. ¿Es eso?».
- **Ronda 2, el mensaje.** Mostrá la plantilla que encaja, con su texto como lo vería la persona, y preguntá si es esa. Si hay dos que sirven, nombrá las dos y recomendá una.
- **Ronda 3, el cuándo.** Cuándo sale y hasta cuándo vale, con tu propuesta.
- **Ronda 4, lo que pasa después.** La repetición y la conversación, juntas. Ejemplo: «Dos cosas más: te propongo que le llegue una sola vez por persona, aunque vuelva a comprar, y que la conversación quede marcada como cliente. ¿Va?».
- **Ronda 5, el OK** (solo con la compra), si no vino antes y no está declarado ni en una propuesta publicada.

Si la persona contesta varias cosas juntas, o cambia de idea, acomodate: las rondas son un orden, no un formulario. Pero no las juntes vos: aunque ya sepas todo, nunca mandes la plantilla, el horario, la repetición y el OK en un solo mensaje. Y el resumen va solo, en su propio mensaje, y esperás la respuesta.

**El mensaje se muestra como lo lee la persona:** con el nombre real del producto en vez de la variable (no «[producto]»).

**Si la propuesta vuelve frenada,** corregila vos (el bloqueo trae qué campo y por qué) y no se lo cuentes a la persona: para ella no cambia nada. Antes de cada intento, releé la forma de un paso en tu SOUL. Si después de tres intentos sigue frenada, decile en una frase que todavía no pudiste dejarla lista y que lo puede revisar el operador, sin códigos. No digas que quedó guardada si no tenés un `proposal_id` que la herramienta te devolvió.

## Si la plantilla no existe

Decilo claro: hoy una plantilla nueva la tiene que crear el equipo a mano en el administrador de WhatsApp de Meta, y Meta la tiene que aprobar (puede tardar de minutos a un par de días). Vos todavía no la podés crear ni mandar a aprobar: decilo así. Ofrecé redactar el texto (con la categoría que corresponde y, si lleva, el botón) para que se la pidan a Meta. El flujo se puede dejar guardado con esa plantilla pendiente, pero no se publica hasta que exista y esté aprobada: decilo antes de armarlo.

## El resumen antes de proponer

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
> - **Nombre:** lo llamo `bienvenida`, y arranca solo para la cohorte de prueba.
>
> ¿Está bien así, o cambio algo?

Si el pedido vino completo, el resumen es igual de corto y marca **lo que completaste vos** («esto no me lo dijiste y lo dejé así: …»). Con el sí, seguí los pasos de tu SOUL: proponer, validar, contar la vista previa y la simulación, y el botón para aprobar. Ese cierre también va corto y sin códigos: el mensaje como lo va a ver la persona, a cuántas personas les habría llegado en la semana, que arranca solo en la cohorte de prueba y quién lo aprueba con el botón. Lo que el sistema no sabe, en una frase con tus palabras (por ejemplo, «no cuenta a quienes ya están en medio de otro mensaje»). Si la propuesta vuelve distinta de lo que confirmó la persona (por ejemplo, otra repetición), corregila antes de contarla.

## El pago rechazado, con sus versiones

El pago rechazado es el único flujo que puede mandar **una versión del mensaje según lo que pasó**, como hace ATT1 con cuatro: a quien ya tenía el descuento, a quien le rechazaron dos veces o más, a quien no le alcanzó el saldo, y a todos los demás. Sigue siendo **un solo mensaje por persona**: lo que pasó elige cuál le llega. Cómo se escribe está en la página de `hotmart`.

- **En la charla**, contá las versiones en lenguaje de negocio («si ya tenía el descuento le llega esta; si le rechazaron dos veces, esta otra…»), cada una con su texto como lo ve la persona, en el orden en que se eligen. La espera es la misma para todas.
- **El botón** lleva el link al pago con el descuento de la instancia ya puesto. Las plantillas tienen que traer ese botón primero.
- **En las ventas** cada versión trae cuántos mensajes salieron y cuántas compras hubo después: «compró después de recibirlo», no «el mensaje causó la compra».

## Si pide algo que el motor no hace

Decí qué parte no se puede, por qué en una línea, y qué se puede hoy. Ejemplos:
- **Dos mensajes seguidos** (uno al toque y otro al día siguiente): hoy un flujo manda un solo mensaje, y tampoco sirve un segundo flujo del mismo momento: dos flujos de la compra no pueden estar prendidos a la vez. No lo ofrezcas como «otro flujo aparte», no le redactes la plantilla al segundo y no lo metas en ninguna propuesta. Ofrecé armar el primero ahora; el segundo todavía no.
- **Otro momento que lo dispare** (por ejemplo, cuando alguien se registra a un webinar o pide un reembolso): hoy un flujo arranca solo con los momentos de tu SOUL; nombralos en una línea, con palabras de negocio, y ofrecé armar uno. No expliques cómo llega cada aviso ni por qué ese no está, y no ofrezcas «explorar» un momento parecido que no resuelve lo que pidió. Lo otro, que se lo pidan al equipo del producto: vos no guardás pedidos ni los anotás.
- **Una condición** («si no abrió el mensaje, mandale otro»): el motor no tiene condiciones todavía, salvo las del pago rechazado (abajo).
- **Algo que hoy hace el legado** (la ficha de pago, la reactivación; el seguimiento con cupón ya se crea como nativo): sigue corriendo en el legado, y ahí no lo podés crear ni cambiar.

No armes una propuesta que sabés que la validación va a frenar.
