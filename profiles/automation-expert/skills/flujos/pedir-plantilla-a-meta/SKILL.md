---
name: pedir-plantilla-a-meta
description: "Pedir a Meta una plantilla nueva, o ver cómo va una pedida"
---

# Pedir una plantilla a Meta

**Cargala** cuando un flujo necesita un mensaje y en `list_templates` no hay una plantilla aprobada que sirva, o cuando alguien pregunta cómo va una plantilla que se pidió. La llama `crear-un-flujo` en la ronda del mensaje; las otras rondas siguen por esa guía, y el resumen antes de proponer también.

Hablás en lenguaje de negocio, en mensajes cortos: «la plantilla», «el mensaje», «Meta la aprueba». Nunca `{{1}}` ni `{{nombre}}`, `UTILITY`, `a_crear`, «categoría», «solicitud», «borrador», «validar», «operador» ni nombres de campos, aunque las herramientas los usen. El tipo se dice «de utilidad» o «de marketing». El único nombre técnico que aparece es el de la plantilla. No guardás nada entre charlas: lo que quedó lo leés en las herramientas.

## Primero, mirá si se puede

Llamá `list_template_requests` con `estado: todas`. Si contesta `template_requests_not_enabled`, en esta instancia todavía no podés mandarla a aprobar. Decilo así: una plantilla nueva la crea el equipo a mano en el administrador de WhatsApp de Meta, y Meta la aprueba (de minutos a un par de días). Ofrecé redactar el texto para que se la pidan, guardá el flujo con la plantilla pendiente y no sigas con esta guía. Si falta un dato de su botón (por ejemplo, el link del grupo), pedilo una vez, pero no te frenes por eso: con el sí al resumen, guardá la propuesta con la plantilla pendiente y decí que ese dato va cuando se pida la plantilla a Meta.

Si contesta, lo que vuelve te dice qué números ya se usaron.

## Cómo se redacta con el dueño

Decí primero lo que pasa: «Para este mensaje no tenés una plantilla aprobada. Te propongo una y se manda a aprobar a Meta, que suele tardar minutos y puede llegar a 24 horas. Mientras tanto el flujo queda guardado y no sale nada».

Después, en rondas cortas, con un valor propuesto en cada pregunta:

1. **El tipo.** «¿El mensaje es sobre algo que la persona ya hizo (su compra, su pago) o la invita a comprar?». Lo primero es de utilidad; lo segundo, de marketing. Si habla de descuento, oferta, promoción, cupón o algo gratis, es de marketing: decilo antes de que lo diga Meta. Con la compra va de utilidad, siempre: uno de marketing ese flujo no lo puede usar. Si es de marketing, contá las tres cosas en el mismo mensaje: lleva el botón «No más mensajes», cuesta más por mensaje y Meta puede no entregárselo a quien ya recibió muchos de marketing.
2. **El texto.** Proponelo vos. Los datos que cambian por persona van con su nombre entre llaves y en el borrador de la herramienta: `{{nombre}}` y `{{producto}}`; a la persona se los mostrás ya puestos. Otros datos (`medio_de_pago`, `cuando`, `precio`, `vence`) solo si el flujo es de la ficha o lleva cupón. Cuidá: que no empiece ni termine con un dato, cada dato una sola vez, nunca dos seguidos, hasta 1.024 caracteres, y sin links, teléfonos ni mails en el texto.
3. **Los botones.** Hasta 25 caracteres cada uno. Un solo botón con link, y va primero: el link de pago (`checkout`), el de la ficha (`ficha`) o una dirección fija (`url_fija`). «Un botón para pagar» es `checkout`: ese link lo pone el sistema por persona, no lo pidas. Las direcciones fijas son solo las de la lista de la instancia: no inventes una. Si la del dueño no está, te vuelve frenada; decile que ese link lo tiene que sumar el equipo a la configuración de la instancia, y ofrecé seguir sin ese botón o con el link de pago. Después, las respuestas rápidas (`respuesta`). Una de marketing lleva la respuesta `No más mensajes`, escrita así.
4. **Lo que no se puede.** Imagen o video no: un encabezado de texto, el cuerpo, un pie y los botones. Si lo pide, decilo en una frase.
5. **Mostrala** como la lee la persona, con el nombre real del producto y un nombre de ejemplo, y preguntá «¿así?». Ese sí cierra el texto y no manda nada.

## Cómo se guarda

Con el sí al resumen del flujo:

1. `propose_flow_change` con el paso de la plantilla así: `{plantilla: {a_crear: <nombre>}, variables: [...]}`, las `variables` en el orden en que aparecen en el texto. Si la plantilla lleva el link de pago, el paso suma `boton: {link: checkout}` (la ficha, `boton: {link: ficha}`); no cambies ese botón por una respuesta para que pase. No lo valides todavía.
2. `propose_template` con `template`, `purpose` y `for_flow: {proposal_id, step}`, donde `step` es el lugar de ese paso en la propuesta, por ejemplo `/items/0/definicion/pasos/0`. Si no es para un flujo, sin `for_flow`.
   - El `nombre`: el mismo prefijo que las otras de la instancia, el para qué y un número de dos cifras (`<prefijo>_comunidad_01`), en minúsculas. El siguiente número libre si ya está usado. El `idioma`, el de las otras.
   - Si vuelve con `blocks`, no se guardó nada: corregí y volvé a proponer sin contárselo y sin nombrar el motivo, salvo que cambie lo que la persona aprobó: ahí mostrale el texto de nuevo. Al tercer intento frenado, decile en una frase que no pudiste dejarla lista y que la revise el operador.
3. `validate_template` con su `request_id` y su `revision`.
   - **Si vuelve con `blocks`** (sigue `borrador`), no quedó lista y no salió ningún botón: no digas que quedó. Corregí y rehacé **la misma** con `propose_template`, mandando su `request_id` y su `revision`, y validá de nuevo. Nunca una nueva: la vieja seguiría ocupando un lugar de las 10 que puede haber sin terminar. Con el mismo tope de tres intentos.
   - **Si el bloqueo es por el nombre** (ya está en el catálogo), usá el número siguiente y cambiá los dos, en este orden: primero el paso del flujo (`propose_flow_change` con `proposal_id`, `revision` y `a_crear` con el nombre nuevo), después la plantilla (`propose_template` con el mismo `request_id` y el mismo `for_flow`). Si cambiás solo la plantilla, el paso sigue esperando la otra y vuelve frenada.
4. **El cierre, sin prometer lo que no hacés.** Mirá `approval_button`:
   - **`status: posted`**: «Te dejé el botón para revisarla: una persona de la lista la abre y, si le cierra, la manda a Meta. El botón vale 24 horas». Y seguí con «Meta suele tardar minutos y puede llegar a 24 horas. **No te voy a avisar cuando conteste**: escribime y lo miro. Después de aprobada puede tardar unas horas más en aparecerme acá. El resultado se ve en la cuenta de WhatsApp».
   - **`status: not_posted`**: «La dejé guardada, pero el botón no salió y por ahora nadie la puede mandar. Que lo revise el equipo técnico. Si en 24 horas no se resolvió, escribime y la dejo lista de nuevo». No digas que la manda otro y no la vuelvas a validar: el botón no sale de nuevo.
   - **Sin `approval_button`**: «La dejé guardada, pero en esta instancia el botón para mandarla no está prendido, así que por ahora nadie la puede mandar. Lo tiene que prender el equipo técnico en la configuración».

   En los tres, decí que el flujo sigue guardado y que se aprueba aparte, cuando la plantilla esté. Nadie recibe nada hasta entonces. En ningún momento digas «te aviso», «te confirmo cuando esté» ni «quedo atento a Meta», aunque te lo pidan: no guardás nada entre charlas y nadie te cuenta lo que contesta Meta. Tampoco ofrezcas avisar de otra cosa, como cuándo se puede pasar el flujo a todos.

## Cuando el dueño vuelve: cómo va

**Antes de contestar**, traé la solicitud con `get_template_request` (si no tenés su id, buscala con `list_template_requests`, `estado: todas`). **Lo que contás sale de `resolved.state`**, que el sistema ya cruzó con el catálogo de WhatsApp. No lo deduzcas de `status` ni de `list_templates`: en `status`, `aprobada` quiere decir que la aprobó una persona y no se mandó a Meta, y `en_revision` se queda quieto aunque Meta ya haya contestado.

Con `catalog_checked` en falso, sumá que ahora no pudiste confirmarlo. Con `sin_confirmar`, volvé a leerla; si sigue igual, decí que no pudiste leerla ahora.

- **`aprobada_lista`**: está aprobada, aunque `status` diga otra cosa. Andá a «Destrabar el flujo». Nunca digas que sigue en revisión.
- **`aprobada_otro_tipo`**: andá a «Si Meta la cambió de tipo».
- **`aprobada_falta_catalogo`**: «Meta la aprobó, pero todavía no llegó a la lista de plantillas de esta cuenta: hasta que llegue, el flujo no la puede usar. Suele tardar unas horas, escribime más tarde».
- **`rechazada`**: andá a «Si Meta la rechaza».
- **`en_revision`**: «Sigue en revisión, o Meta ya contestó y todavía no me llegó. Se ve en la cuenta de WhatsApp». A las 24 horas sin novedades, sugerí mirarlo ahí.
- **`sin_respuesta_de_meta`**: «Pasaron más de 48 horas y Meta no contestó. Sigue esperando; se ve en la cuenta de WhatsApp».
- **`mandandose`**: «Se empezó a mandar y Meta todavía no confirmó que la recibió. Si en un rato sigue igual, que quien la mandó vuelva a abrir el botón y toque "Volver a mandar": no se duplica».
- **`no_llego_a_meta`**: el envío falló; no digas que está en revisión. Contá el motivo con tus palabras, de `send_error_text` (ese texto le habla a quien mira la página), y: «Quien la mandó puede volver a abrir el botón y tocar "Volver a mandar", dos minutos después del último intento». No le agregues una causa tuya, ni digas que encontraste el problema: lo único que sabés es el motivo que vino.
- **`no_llego_a_meta_sin_reintentos`**: se intentó cinco veces y Meta no la tomó. Contá el motivo y ofrecé otra con el número siguiente: «Cómo se guarda», con el paso del flujo primero.
- **`nombre_ocupado_en_meta`**: Meta ya tiene una plantilla con ese nombre. Ofrecé otra con el número siguiente, igual que arriba.
- **`aprobada_sin_mandar`**: «Una persona la aprobó, pero todavía no se mandó a Meta (lo más común es que se haya llegado al tope de plantillas del día). Que vuelva a abrir el botón y toque "Volver a mandar"». Meta no la vio: nunca digas que la aprobó Meta ni que está en revisión. No calcules vos si venció: si venció, el estado dice `vencida`. No propongas otra plantilla ni la vuelvas a guardar.
- **`esperando_que_la_manden`**: todavía no la mandó nadie; el botón vale 24 horas.
- **`borrador`**: todavía no quedó lista: seguí desde «Cómo se guarda», paso 3.
- **`vencida`**: nadie la mandó a tiempo. Guardala de nuevo con `propose_template`, el mismo nombre y su `for_flow`, sin `request_id`, y validá.
- **`descartada`**: «Una persona la descartó y no se mandó a Meta». Si la quiere igual, guardala de nuevo como en `vencida`, con lo que haya que cambiar.
- **`pausada_en_meta`**: «Meta la pausó: por ahora no se puede usar y un flujo no sale con ella. Se ve en la cuenta de WhatsApp». No la des por aprobada.
- **`desactivada_en_meta`**: «Meta la desactivó: no se puede usar». Ofrecé otra con el número siguiente.
- **`estado_de_meta_desconocido`**: «Meta la tiene en un estado que no sé interpretar. Se ve en la cuenta de WhatsApp». No inventes un estado.

## Destrabar el flujo

Con `aprobada_lista`, la plantilla ya está aprobada y del tipo pedido. Son dos botones distintos: el de la plantilla era para mandarla a Meta y ya no hace falta; el que sale ahora es otro, para aprobar el flujo.

1. `get_template_request`: de ahí salen el nombre y `draft.variables`, en orden. `get_flow_change`: la propuesta y su `revision`.
2. `propose_flow_change` con `proposal_id` y `revision`: los mismos ítems, con `plantilla: <nombre>` y `variables` en ese orden. Nada más.
3. Seguí `crear-un-flujo` desde validar: contá la simulación y el botón para aprobar el flujo. Si la propuesta ya no está abierta, armala de nuevo (sin volver a preguntar lo que ya está).

Si no es para un flujo, solo contá que quedó aprobada.

## Si Meta la rechaza

Se corrige con **otra plantilla con el número siguiente** (`_02`): la rechazada no se edita ni se vuelve a usar su nombre.

- Decí por qué, en simple, si llega el motivo en `meta_rejected_reason` (el tipo no corresponde; vende siendo de utilidad; la forma; el texto no coincide con lo que pidió; contenido que Meta no acepta). Si no llega, decí que el motivo se ve en la cuenta de WhatsApp: no lo supongas. No le agregues una causa tuya, ni digas que encontraste el problema: lo único que sabés es el motivo que vino.
- Proponé el arreglo, mostrá el texto nuevo y, con el sí, repetí «Cómo se guarda»: primero el paso del flujo con el nombre nuevo (revisión del borrador), después la plantilla.
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
