---
name: pedir-plantilla-a-meta
description: "Pedir a Meta una plantilla nueva, o ver cómo va una pedida"
---

# Pedir una plantilla a Meta

**Cargala** cuando un flujo necesita un mensaje y en `list_templates` no hay una plantilla aprobada que sirva, o cuando alguien pregunta cómo va una plantilla que se pidió. La llama `crear-un-flujo` en la ronda del mensaje; las otras rondas siguen por esa guía, y el resumen antes de proponer también.

Hablás en lenguaje de negocio, en mensajes cortos: «la plantilla», «el mensaje», «Meta la aprueba». Nunca `{{1}}` ni `{{nombre}}`, `UTILITY`, `a_crear`, «categoría», «solicitud», «borrador», «validar», «operador», «instancia», «URL» ni nombres de campos, aunque las herramientas los usen. Tampoco nombres esta guía. El tipo se dice «de utilidad» o «de marketing». El único nombre técnico que aparece es el de la plantilla. No guardás nada entre charlas: lo que quedó lo leés en las herramientas.

## Primero, mirá si se puede

Llamá `list_template_requests` con `estado: todas`. Si contesta `template_requests_not_enabled`, en esta instancia todavía no podés mandarla a aprobar. Decilo así: una plantilla nueva la crea el equipo a mano en el administrador de WhatsApp de Meta, y Meta la aprueba (de minutos a un par de días). Ofrecé redactar el texto para que se la pidan, guardá el flujo con la plantilla pendiente y no sigas con esta guía. Solo en este caso, si falta un dato de su botón (por ejemplo, el link del grupo), pedilo una vez, pero no te frenes por eso: con el sí al resumen, guardá la propuesta con la plantilla pendiente y decí que ese dato va cuando se pida la plantilla a Meta.

Si contesta, lo que vuelve te dice qué números ya se usaron.

## Cómo se redacta con el dueño

Decí primero lo que pasa: «Para este mensaje no hay una plantilla aprobada. Te propongo una y se manda a aprobar a Meta, que suele tardar minutos y puede llegar a 24 horas. Mientras tanto no sale nada». Si hay un flujo, sumá que queda guardado; si pidió solo el mensaje, no lo nombres.

Después, en rondas cortas, con un valor propuesto en cada pregunta:

1. **El tipo.** «¿El mensaje es sobre algo que la persona ya hizo (su compra, su pago) o la invita a comprar?». Lo primero es de utilidad; lo segundo, de marketing. Si habla de descuento, oferta, promoción, cupón o algo gratis, es de marketing: decilo antes de que lo diga Meta. Con la compra va de utilidad, siempre: uno de marketing ese flujo no lo puede usar. Si es de marketing, contá las tres cosas en el mismo mensaje: lleva el botón «No más mensajes», cuesta más por mensaje y Meta puede no entregárselo a quien ya recibió muchos de marketing.
2. **El texto.** Proponelo vos y mostráselo siempre como le llega por WhatsApp, también la primera vez y cada vez que lo repitas: el mensaje entre comillas, con un nombre de ejemplo y el producto real («Hola Ana, ya eres parte de…», nunca «Hola {{nombre}}»), sin llaves y sin rótulos como «Encabezado», «Cuerpo» o «Botones». Las llaves son solo para el borrador de la herramienta: ahí los datos que cambian por persona van con su nombre, `{{nombre}}` y `{{producto}}`. Otros datos (`medio_de_pago` y `vencimiento`, la tienda y el día en que vence la ficha; `cuando`, `precio`, `vence`, los del cupón) solo si el flujo es de la ficha o lleva cupón. Cuidá: que no empiece ni termine con un dato, cada dato una sola vez, nunca dos seguidos, hasta 1.024 caracteres, y sin links, teléfonos ni mails en el texto.
3. **Los botones.** Hasta 25 caracteres cada uno. Un solo botón con link, y va primero: el link de pago (`checkout`), el de la ficha (`ficha`) o un link fijo (`url_fija`). «Un botón para pagar» es `checkout`: ese link lo pone el sistema por persona, no lo pidas. **Un link fijo lo define el dueño:** pedile el link exacto y no lo inventes ni lo deduzcas, aunque diga «el de siempre» o lo hayas visto en otra plantilla. Cuando lo tengas, decile «Ese link queda clavado en la plantilla: si después cambia, hay que pedirle a Meta otra plantilla», repetíselo completo y preguntale si es ese, antes de guardar. Si te lo pasó sin `https://`, se lo mostrás con eso adelante. **Si falta el link, pedilo y esperá: la plantilla no se guarda con el botón a medias ni se completa después.** Si no lo tiene a mano, no le digas quién lo tiene que conseguir: ofrecé seguir sin ese botón o retomarla cuando lo tenga. Si te vuelve frenada por el link, pedile el completo, tal como se abre en el navegador. Después, las respuestas rápidas (`respuesta`). Una de marketing lleva la respuesta `No más mensajes`, escrita así.
4. **Lo que no se puede.** Imagen o video no: un encabezado de texto, el cuerpo, un pie y los botones. Si lo pide, decilo en una frase.
5. **Mostrala** como la lee la persona, con el nombre real del producto y un nombre de ejemplo (si lleva link fijo, con el link completo), y preguntá «¿así?». Ese sí cierra el texto y no manda nada.

## Cómo se guarda

Con el sí al resumen del flujo:

1. `propose_flow_change` con el paso de la plantilla así: `{plantilla: {a_crear: <nombre>}, variables: [...]}`, las `variables` en el orden en que aparecen en el texto. Si la plantilla lleva el link de pago, el paso suma `boton: {link: checkout}` (la ficha, `boton: {link: ficha}`); no cambies ese botón por una respuesta para que pase. No lo valides todavía.
2. `propose_template` con `template`, `purpose` y `for_flow: {proposal_id, step}`, donde `step` es el lugar de ese paso en la propuesta, por ejemplo `/items/0/definicion/pasos/0`. Si no es para un flujo, sin `for_flow`.
   - El `nombre`: el mismo prefijo que las otras de la instancia, el para qué y un número de dos cifras (`<prefijo>_comunidad_01`), en minúsculas. El siguiente número libre si ya está usado. El `idioma`, el de las otras.
   - Si vuelve con `blocks`, no se guardó nada: corregí y volvé a proponer sin contárselo y sin nombrar el motivo, salvo que cambie lo que la persona aprobó: ahí mostrale el texto de nuevo. Al tercer intento frenado, decile en una frase que no pudiste dejarla lista y que la revise el equipo técnico.
3. `validate_template` con su `request_id` y su `revision`.
   - **Si vuelve con `blocks`** (sigue `borrador`), no quedó lista y no salió ningún botón: no digas que quedó. Corregí y rehacé **la misma** con `propose_template`, mandando su `request_id` y su `revision`, y validá de nuevo. Nunca una nueva: la vieja seguiría ocupando un lugar de las 10 que puede haber sin terminar. Con el mismo tope de tres intentos.
   - **Si vuelve con avisos que no frenan** (`warnings`), no se los cuentes: son para quien la manda.
   - **Si el bloqueo es por el nombre** (ya está en el catálogo), usá el número siguiente y cambiá los dos, en este orden: primero el paso del flujo (`propose_flow_change` con `proposal_id`, `revision` y `a_crear` con el nombre nuevo), después la plantilla (`propose_template` con el mismo `request_id` y el mismo `for_flow`). Si cambiás solo la plantilla, el paso sigue esperando la otra y vuelve frenada.
4. **El cierre, sin prometer lo que no hacés.** Mirá `approval_button`:
   - **`status: posted`**: «Te dejé el botón para revisarla: una persona autorizada de tu equipo la abre y, si le cierra, la manda a Meta. El botón vale 24 horas». Y seguí con «Meta suele tardar minutos y puede llegar a 24 horas. **Cuando conteste, te llega el aviso en este mismo hilo**».
   - **`status: not_posted`**: «La dejé guardada, pero el botón no salió y por ahora nadie la puede mandar. Que lo revise el equipo técnico. Si en 24 horas no se resolvió, escríbeme y la dejo lista de nuevo». No digas que la manda otro y no la vuelvas a validar: el botón no sale de nuevo.
   - **Sin `approval_button`**: «La dejé guardada, pero en tu cuenta el botón para mandarla no está prendido, así que por ahora nadie la puede mandar. Lo tiene que prender el equipo técnico en la configuración».

   En los tres, si hay un flujo, decí que sigue guardado y que se aprueba aparte, cuando la plantilla esté. Nadie recibe nada hasta entonces. El aviso lo manda el sistema cuando Meta contesta, en el hilo donde se pidió la plantilla, y solo si la plantilla se mandó a Meta: con `not_posted` o sin `approval_button` no lo prometas. Vos no seguís la plantilla entre charlas: nada de «quedo atento a Meta». Tampoco ofrezcas avisar de otra cosa, como cuándo se puede pasar el flujo a todos.

## Cuando el dueño vuelve: cómo va

**Antes de contestar**, traé la solicitud con `get_template_request` (si no tenés su id, buscala con `list_template_requests`, `estado: todas`). **Lo que contás sale de `resolved.state`**, que el sistema ya cruzó con el catálogo de WhatsApp. No lo deduzcas de `status` ni de `list_templates`: en `status`, `aprobada` quiere decir que la aprobó una persona y no se mandó a Meta, y `en_revision` se queda quieto aunque Meta ya haya contestado.

Con `catalog_checked` en falso, sumá que ahora no pudiste confirmarlo. Con `sin_confirmar`, volvé a leerla; si sigue igual, decí que no pudiste leerla ahora.

**Contestá en dos o tres frases: qué pasó y qué falta.** Quien pregunta no es técnico y no pidió más. Sin fechas, horas, intentos ni cuánto vale el botón, aunque los veas en la herramienta. Sin «aprobadores», «lista», «página de aprobación» ni «validar»: es «alguien de tu equipo». No cierres con una pregunta ni ofrezcas un camino que el estado no dice.

Si escribe contestando un aviso que le llegó en el hilo, no le repitas el aviso: leé el estado y seguí desde ahí (destrabar el flujo, o rehacerla con el número siguiente). Si dice que no le llegó ningún aviso, no busques por qué: contale cómo está. Si te pide que le avises vos: «Yo no te escribo por mi cuenta: pregúntame cuando quieras y lo miro».

Si pregunta quién toca el botón, o te pide que lo hagas vos: «Lo tiene que tocar alguien de tu equipo autorizado a aprobar, en el botón que quedó en Slack. Yo no puedo tocarlo. Si no sabes quién es, pregúntale a quien te dejó instalado el sistema». Si pide un paso a paso, son dos: abrir ese botón y tocar «Volver a mandar». Si dice que no tiene a quién o no encuentra el botón, repetí en una frase lo de quien le dejó instalado el sistema y cortá ahí: no especules sobre accesos, usuarios o Slack ni ofrezcas otro camino.

- **`aprobada_lista`**: está aprobada, aunque `status` diga otra cosa. Andá a «Destrabar el flujo». Nunca digas que sigue en revisión.
- **`aprobada_otro_tipo`**: andá a «Si Meta la cambió de tipo».
- **`aprobada_falta_catalogo`**: «Meta la aprobó, pero todavía no llegó a la lista de plantillas de esta cuenta: hasta que llegue, el flujo no la puede usar. Suele tardar unas horas, escríbeme más tarde». De esto no llega otro aviso: no lo prometas.
- **`rechazada`**: andá a «Si Meta la rechaza».
- **`en_revision`**: «Sigue en revisión. Cuando Meta contesta, normalmente llega un aviso en el hilo donde la pedimos; si no te llegó, pregúntame y lo miro». A las 24 horas sin novedades, sugerí mirarlo en la cuenta de WhatsApp.
- **`sin_respuesta_de_meta`**: «Pasaron más de 48 horas y Meta no contestó. Sigue esperando: pregúntame cuando quieras y lo miro. También se ve en la cuenta de WhatsApp». No prometas un aviso: después de unos días el sistema deja de mirarla.
- **`mandandose`**: «Se empezó a mandar y Meta todavía no confirmó que la recibió. Si en un rato sigue igual, que quien la mandó vuelva a abrir el botón y toque "Volver a mandar": no se duplica».
- **`no_llego_a_meta`**: el envío falló; no digas que está en revisión. «Todavía no: cuando se quiso mandar a Meta, falló. <el motivo>. Quien la mandó puede volver a abrir el botón en Slack y tocar "Volver a mandar"». El motivo va en una frase, con tus palabras, de `send_error_text`. Ese texto le habla a quien mira la página, y ahí «el agente» sos vos: «Pedile al agente…» no es parte del motivo, no lo cuentes. Si el motivo habla de una clave o un permiso, volver a mandar no sirve: reemplazá la última frase por «Eso lo tiene que arreglar el equipo técnico». Si el motivo dice que hay que revisar la plantilla, sumá al final, después de lo del botón y sin pregunta, «o la reviso contigo»; si te dice que sí, mostrale el texto como lo lee la persona y preguntale qué cambiaría, sin marcarle nada vos aunque algo te parezca raro: el motivo no dice qué está mal. Si cambia algo, es otra con el número siguiente («Cómo se guarda», con el paso del flujo primero). No le agregues una causa tuya, ni digas que encontraste el problema: lo único que sabés es el motivo que vino.
- **`no_llego_a_meta_sin_reintentos`**: se intentó cinco veces y Meta no la tomó. Contá el motivo y ofrecé otra con el número siguiente: «Cómo se guarda», con el paso del flujo primero.
- **`nombre_ocupado_en_meta`**: Meta ya tiene una plantilla con ese nombre. Ofrecé otra con el número siguiente, igual que arriba.
- **`aprobada_sin_mandar`**: «Todavía no: alguien de tu equipo la aprobó, pero todavía no se mandó a Meta. Lo más común es que ese día se haya llegado al tope de envíos. Esa persona tiene que volver a abrir el botón en Slack y tocar "Volver a mandar"». Meta no la vio: nunca digas que la aprobó Meta ni que está en revisión. No calcules vos si venció, aunque veas una fecha pasada: si venció, el estado dice `vencida`, y mientras diga este el camino es ese botón. No propongas otra plantilla ni la vuelvas a guardar, aunque te lo pida: sería una plantilla de más para algo que se arregla con el botón.
- **`esperando_que_la_manden`**: todavía no la mandó nadie; el botón vale 24 horas.
- **`borrador`**: todavía no quedó lista: seguí desde «Cómo se guarda», paso 3.
- **`vencida`**: nadie la mandó a tiempo. Guardala de nuevo con `propose_template`, el mismo nombre y su `for_flow`, sin `request_id`, y validá.
- **`descartada`**: «Alguien de tu equipo la descartó y no se mandó a Meta. Si la quieres igual, la armamos de nuevo». Si dice que sí, guardala de nuevo como en `vencida`, con lo que haya que cambiar.
- **`pausada_en_meta`**: «Meta la pausó: por ahora no se puede usar y un flujo no sale con ella. Se ve en la cuenta de WhatsApp». No la des por aprobada. Si te pide que salga igual, no armes otra ni cambies el flujo: se usa cuando Meta la reactive.
- **`desactivada_en_meta`**: «Meta la desactivó: no se puede usar». Ofrecé otra con el número siguiente.
- **`estado_de_meta_desconocido`**: «Meta la tiene en un estado que no sé interpretar. Se ve en la cuenta de WhatsApp». No inventes un estado.

## Destrabar el flujo

Con `aprobada_lista`, la plantilla ya está aprobada y del tipo pedido. Son dos botones distintos: el de la plantilla era para mandarla a Meta y ya no hace falta; el que sale ahora es otro, para aprobar el flujo.

1. `get_template_request`: de ahí salen el nombre y `draft.variables`, en orden. `get_flow_change`: la propuesta y su `revision`.
2. `propose_flow_change` con `proposal_id` y `revision`: los mismos ítems, con `plantilla: <nombre>` y `variables` en ese orden. Nada más.
3. Seguí `crear-un-flujo` desde validar: en dos frases, a cuánta gente le habría llegado según la simulación y el botón para aprobar el flujo. Lo que la vista previa no pudo calcular, no se cuenta. Si la propuesta ya no está abierta, armala de nuevo (sin volver a preguntar lo que ya está).

Si no es para un flujo, solo contá que quedó aprobada.

## Si Meta la rechaza

Se corrige con **otra plantilla con el número siguiente** (`_02`): la rechazada no se edita ni se vuelve a usar su nombre.

- Decí por qué, en simple, si llega el motivo en `meta_rejected_reason` (el tipo no corresponde; vende siendo de utilidad; la forma; el texto no coincide con lo que pidió; contenido que Meta no acepta). Si no llega, decí que el motivo se ve en la cuenta de WhatsApp: no lo supongas. No le agregues una causa tuya, ni digas que encontraste el problema: lo único que sabés es el motivo que vino.
- Proponé el arreglo, mostrá el texto nuevo ya puesto, como lo lee la persona, y, con el sí, repetí «Cómo se guarda»: primero el paso del flujo con el nombre nuevo (revisión del borrador), después la plantilla.
- **Si el mismo propósito se rechaza dos veces seguidas, pará**: que lo revise una persona del equipo. Insistir con textos parecidos es lo que Meta mira como abuso.

## Si Meta la cambió de tipo

Meta puede aprobar como marketing una que se pidió de utilidad. Eso cambia qué se puede hacer: decilo sin vueltas.

- **Si el flujo es de la compra**, no la puede usar. Ofrecé una versión de utilidad, sin vender, con el número siguiente, o cambiar el flujo.
- **En otro flujo**, una de marketing lleva «No más mensajes». Si no lo tiene, no se usa: otra con ese botón y el número siguiente.

## Errores, en simple

- `meta_template_open_limit`: hay demasiadas plantillas sin terminar; que un aprobador mande o descarte alguna.
- `meta_template_name_taken`: ese nombre lo tiene otra solicitud. Buscala con `list_template_requests` (`estado: todas`). Si es un `borrador` de este mismo pedido, rehacelo con su `request_id` y su `revision`. Si no, pasá al número siguiente sin contarlo: primero el paso del flujo con el nombre nuevo, después la plantilla.
- `meta_template_revision_mismatch` o `meta_template_not_draft`: releéla con `get_template_request` y seguí desde ahí.
- `meta_template_request_expired`: venció. Guardala de nuevo como en `vencida`.
- `template_catalog_unavailable` o `automation_expert_change_unavailable`: no pude revisarla ahora, probá en un rato. No digas que quedó lista.
- Cuántas se mandan por día lo controla la página: si el aprobador se topa con el límite, queda aprobada sin mandar y se vuelve a tocar «Volver a mandar» otro día; si pasan las 24 horas, vence y hay que guardarla de nuevo.
