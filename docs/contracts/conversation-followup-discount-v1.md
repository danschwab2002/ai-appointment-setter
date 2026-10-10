# Contrato: seguimiento con cupon para quien contesto y no compro (v1)

- **Decision:** Dan, 2026-09-28 (vault: `productos/soporte-infoproductores/decisiones/2026-09-28-seguimiento-con-cupon-solo-a-quien-contesto.md`). Con manifiesto: Dan, 2026-10-09 y 2026-10-10 (vault: `productos/soporte-infoproductores/decisiones/2026-10-09-el-cupon-de-att1-sale-como-en-johanna-solo-a-quien-contesto.md` y `productos/soporte-infoproductores/decisiones/2026-10-10-el-cupon-de-att1-va-con-los-defaults-recomendados.md`), y [ADR-0023](../decisions/0023-el-cupon-con-manifiesto-reusa-descuento.md), propuesto.
- **Codigo:** `src/bridge/followup_discount.py`, migracion `20260928000400_conversation_followup_discount_v1.sql`. Con manifiesto, ademas la migracion `20261010000100_portable_conversation_followup_claim.sql` (bridge 1.5.0, sin publicar).
- **Gate:** `CONVERSATION_FOLLOWUP_ENABLED`, default `false`. Con manifiesto, ademas `[flujos].descuento = true`.

## Que hace

A quien nos escribio al menos una vez, recibio nuestra respuesta y se quedo callado 24 h sin comprar, le manda **una** plantilla aprobada de Meta con un 10 % de descuento y un boton que abre el checkout de Hotmart con el cupon ya cargado (`offDiscount`). Uno solo por conversacion, de por vida.

A quien nunca contesto no le manda nada. Plantillas de marketing a quien no interactua degradan la cuenta de Meta; esa es la regla que ordena todo lo demas.

## A quien (todo junto)

El bridge evalua contra Chatwoot, porque ahi viven esos hechos:

1. La conversacion esta `open` o `resolved`, sin `muted` ni `snoozed_until`. `pending` y `snoozed` se saltean con `conversation_status_excluded`. Una resuelta entra porque el equipo resuelve a mano para ordenar su bandeja, tambien lo que atendio solo el agente: resolver no dice nada del lead. La pausa, la derivacion y el opt-out si, y siguen siendo barreras (ver "Como barre").
2. No tiene la etiqueta `automation_opted_out` ni la etiqueta `automation_paused`. Pausada quiere decir en manos del equipo.
3. El contacto no esta bloqueado, tiene telefono E.164 y tiene nombre.
4. El lead escribio al menos un mensaje. Si no, se saltea con `never_replied`.
5. El ultimo mensaje conversacional es nuestro y es del AgentBot. Si es del lead, se saltea con `last_message_inbound`: el turno es del agente, y si nadie contesta le toca a la reactivacion. Si es de una persona del equipo, se saltea con `last_message_not_from_agent`.
6. Ninguna persona del equipo escribio despues del ultimo mensaje del lead.
7. Pasaron entre 24 h y 72 h desde el ultimo mensaje del lead. El techo evita escribirle al historico el dia que se prende el flag.

La base evalua al reservar (`claim_conversation_followup_v1`) lo que tiene que sobrevivir a un bridge mal configurado:

- no hay otro seguimiento vivo en la conversacion;
- no hay una derivacion sin atender (mismo predicado que la reactivacion desde `20260927000300`);
- no hay `human_takeover` ni estado pausado;
- el contacto no esta en `opted_out`, `blocked` ni `restricted`;
- no hay compra del lead en `purchase_intents` (`purchased`) ni en las identidades de `PURCHASE_APPROVED`/`PURCHASE_COMPLETE`. Se busca por mail o por los ultimos 9 digitos del telefono, porque Hotmart puede guardarlo sin codigo de pais.

Con manifiesto la reserva es `claim_portable_conversation_followup_v1`: las mismas barreras y una mas, la conversacion adoptada (ver "En un runtime con manifiesto").

Justo antes de reservar, el bridge relee la conversacion. Si el lead escribio en el medio, no se reserva nada (`lead_wrote_meanwhile`).

## Como barre

`ChatwootClient.list_recent_conversations_with_messages` pide `status=open` y despues `status=resolved`, cada uno con `sort_by=last_activity_at_desc`, y corta cada lista en la primera conversacion con `last_activity_at` anterior a `ahora - CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS - 1 h`. De ahi para abajo todas son mas viejas, y ninguna puede tener un mensaje del lead dentro de la ventana: ningun mensaje es posterior a `last_activity_at`, y resolver la actualiza. Por cada conversacion arriba del corte trae el show y el historial.

- Falla cerrado (`conversation_scan_incomplete`) si `CONVERSATION_FOLLOWUP_MAX_PAGES`, que cuenta por estado, se agota antes del corte o del final de la lista.
- Tambien falla cerrado si un item no trae `last_activity_at` entero (`invalid_conversation_activity`) o si la lista no viene en el orden pedido (`conversation_order_unexpected`): sin eso el corte no es confiable. Un barrido incompleto que no falla se leeria como "no hay a quien".
- Una conversacion que aparece en los dos estados (el equipo la resolvio entre los dos listados) se lee una vez.
- Una conversacion que recibe actividad durante el barrido sube al principio de su lista y puede quedar afuera de esa pasada; la toma la siguiente, a los `CONVERSATION_FOLLOWUP_INTERVAL_SECONDS`.

Mandar la plantilla no reabre una conversacion resuelta: Chatwoot 4.13 solo reabre con un mensaje entrante (`Message#reopen_conversation`). Si el lead contesta, la conversacion se abre y el agente la atiende como cualquier respuesta.

Medido el 2026-10-01 en el inbox 9 (`docs/operations/2026-10-01-seguimiento-con-cupon-primer-envio.md`): las 149 conversaciones resueltas las resolvio a mano el equipo, y de las 11 que atendio solo el agente en 10 dias resolvio 8, 7 antes de las 72 h. Con el barrido de solo abiertas, desde la activacion el seguimiento salio una vez y perdio tres candidatas.

## Regimenes

Solo sirven para auditar. Todos reciben la misma plantilla.

| Regimen | Cuando |
|---|---|
| `link_sent_no_purchase` | Algun mensaje nuestro lleva un link `https://pay.hotmart.com/` |
| `went_quiet` | Pregunto, le contestamos y se callo, sin link |
| `payment_failed` | Recibio la plantilla de pago fallido. Se reconoce por el texto «no pudo completarse»: la API de mensajes de Chatwoot no devuelve `template_params` |

Diferidos por Dan: el lead que pidio descuento (hoy se deriva con `commercial_exception`) y el que dijo «mas adelante». Nunca reciben seguimiento: derivados o pausados, opt-out, y quienes ya compraron.

## El link

Lo emite la misma RPC que el link del agente (`reserve_chatwoot_checkout_issuance_v2`; con manifiesto, `reserve_portable_checkout_issuance_v2`), anclada en el ID de **nuestro** ultimo mensaje (`trigger_external_message_id`). Los IDs de Chatwoot son globales, asi que nunca coinciden con el de un mensaje del lead: el seguimiento siempre es una emision nueva, con su propio ULID. Lleva lo mismo que el link del agente:

```
https://pay.hotmart.com/<producto>?off=<oferta del lead>&checkoutMode=10&src=hermes&sck=<sck del anuncio>~hermes~v1~<ULID>[&fbclid=<fbclid>]
```

Desde la migracion `20261005000100` (ADR-0022) el marcador se separa con `~`, que viaja literal. Los links emitidos antes llevan `%7Chermes%7Cv1%7C<ULID>`, y una reserva anterior que el seguimiento reusa sale asi: los lectores aceptan las dos formas. La `|` del `sck` de un anuncio de un linaje viejo se sigue encodeando a `%7C`.

- `checkout_url_final` conserva su contrato: la correlacion de la compra, la revision diaria y el recuperador lo leen sin cambios.
- El cupon **no** entra en esa URL. El bridge arma el boton con todo lo que va despues de `https://pay.hotmart.com/` mas `&offDiscount=<cupon>`.
- El codigo del cupon queda en `conversation_followup_events.coupon_code`.

## La plantilla

`parse_followup_template` la lee del catalogo del inbox en cada barrido y falla cerrado si no cumple todo esto:

- esta `APPROVED`;
- el idioma coincide con `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` si esta declarada;
- el cuerpo tiene exactamente los marcadores `{{1}}` (primer nombre) y `{{2}}` (producto), y `{{3}}` (cupon) o ningun otro. Con `{{3}}` el codigo va tambien en el texto: es la de Johanna, `johanna_seguimiento_descuento_01`. Sin `{{3}}` va solo en el boton: es la de ATT1, `att1_seguimiento_descuento_01` (decision de Dan del 2026-10-01). Cualquier otro conjunto (solo `{{1}}`, o `{{1}}`, `{{2}}` y `{{4}}`) falla con `followup_template_unexpected_placeholders`;
- tiene exactamente un boton, de tipo `URL`, con `url = https://pay.hotmart.com/{{1}}`. En las dos plantillas el cupon va en el boton.

El envio va por `processed_params.buttons = [{type: "url", parameter: <sufijo>}]`, que Chatwoot 4.13 traduce al componente del boton (`Whatsapp::TemplateProcessorService#process_button_components`). El primer nombre sale de la cadena de `bridge.lead_first_name`.

`processed_params.body` lleva un parametro por marcador: `1` y `2`, y `3` solo si la plantilla lo tiene. Meta rechaza con `#132000` un cuerpo con otra cantidad de parametros que la plantilla, y lo hace despues de que Chatwoot acepto el mensaje (medido con uno de menos el 2026-08-31, `docs/operations/2026-08-31-precheckout-production-activation.md`): el seguimiento quedaria gastado sin haber llegado.

Las dos plantillas estan capturadas del catalogo por la API: la de Johanna en `tests/fixtures/chatwoot_message_templates_inbox_9_20261001.json` (idioma `en`) y la de ATT1 en `tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json` (`es_MX`, `MARKETING`, con el valor de `offDiscount` redactado).

## El ciclo de un envio

Cada paso deja su registro:

1. `claim_conversation_followup_v1` (con manifiesto, `claim_portable_conversation_followup_v1`) aplica las barreras y emite el link. La fila del seguimiento queda `claimed` y la emision `reserved`.
2. `authorize_chatwoot_checkout_issuance_v2`, con el mismo ancla y la misma identidad con que se reservo, pasa la emision a `request_started`.
3. El AgentBot manda la plantilla con la marca `content_attributes.conversation_followup_command_key` (`followup:<conversacion>:<nuestro ultimo mensaje>`).
4. Si sale bien: la emision queda `accepted_by_chatwoot` y el seguimiento `sent`.
5. Si Chatwoot falla o responde algo incierto: la emision queda `delivery_unknown` y el seguimiento `failed`. La siguiente reserva recibe `issuance_delivery_unknown` y no vuelve a mandar nada, porque mandar dos veces un cupon es peor que no mandarlo.

Un fallo **antes** de autorizar libera la fila, y el proximo barrido reusa el mismo link.

## Por que no se pisa con los otros motores

| Estado de la conversacion | Dueño |
|---|---|
| El lead escribio ultimo, dentro de la ventana | El agente (y el monitor de estancadas) |
| El lead escribio ultimo, mas de 24 h sin respuesta | La reactivacion |
| Escribimos nosotros y el lead se callo entre 24 y 72 h, sin compra | **Este seguimiento** |
| Derivada o pausada | El equipo: ningun motor automatico |

La reactivacion exige que el ultimo mensaje sea del lead y el seguimiento que sea nuestro, asi que no pueden elegir la misma conversacion en el mismo barrido. Los tests lo fijan en las dos direcciones sobre las conversaciones capturadas. Si el lead contesta el seguimiento y nadie le responde, la reactivacion tiene que poder actuar: por eso no hay una guarda que la bloquee.

## En un runtime con manifiesto (bridge 1.5.0)

Hasta la 1.4.0 el flag frenaba el arranque de todo runtime portable (`ATT1 runtime capabilities are not portable`). Desde la 1.5.0 el seguimiento corre con un manifiesto v2 (`INSTANCE_MANIFEST_PATH`), con las diferencias de esta seccion. Sin manifiesto (Johanna) todo queda como dicen las anteriores: la misma RPC con los mismos argumentos, a cualquier hora, a todo el inbox y con el nombre completo cuando no hay primer nombre.

- **La identidad.** El telefono del contacto de Chatwoot es el del `wa_id` (en Mexico, `521` + 10 digitos), y la base puede conocer a esa persona por la otra forma: el primer contacto de ATT1 toma la del formulario (`52` + 10) y el carrito la que trae Hotmart, con el 1 o sin el. La reserva y la autorizacion exigen la identidad exacta del caso. Con el `wa_id` y el caso en `52`, la reserva compartida da `issuance_blocked_identity` y el cupon no saldria nunca; con el caso en `521` y la intencion del formulario en `52`, saldria con la oferta por defecto y fabricaria una segunda intencion viva. El barredor resuelve la identidad con el mismo resolvedor del entrante (`resolve_inbound_external_user_id`, ver [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md), equivalencia de telefonos): si entre las dos formas del movil hay exactamente una identidad activa del inbox y no es la textual, usa la guardada. La resuelta va a la reserva y a la autorizacion. Si la lectura falla, o devuelve algo que no es una forma del mismo movil, no se reserva nada: el intento cuenta como fallido (`identity_lookup_failed`) y el barrido queda `error`.
- **La reserva.** Va por `claim_portable_conversation_followup_v1` (migracion `20261010000100`), derivada de la definicion vigente de `claim_conversation_followup_v1`: la misma firma, el mismo resultado y las mismas barreras, con el link emitido por `reserve_portable_checkout_issuance_v2`, que busca la intencion, la compra previa y el opt-out por las dos formas del movil. Asi el cupon lleva la oferta, el `sck` y el `fbclid` del formulario, como el link del agente. Solo `service_role` la ejecuta.
- **Solo conversaciones adoptadas.** La reserva portable suma una barrera: si la conversacion no tiene el evento `inbound_adopted_template_conversation` (la respuesta a una plantilla nuestra del piloto: carrito, primer contacto o pago fallido), devuelve `blocked_not_template_reply` y no escribe nada. Quien nos escribio por su cuenta no recibe el cupon, ni nadie en una conversacion que la admision no adopto; en Johanna si lo recibe. Es la politica `later_step` aprobada para ATT1 (`att1-commercial-006-discount` en `docs/design/att1-commercial-information-approval-v1.md`): el 10 % solo despues de una respuesta a la plantilla inicial. La barrera va despues del replay, del bloqueo de la conversacion y del limite de uno por conversacion, y antes del conteo de casos, la derivacion, la compra y la reserva del link. El barredor la cuenta como un motivo mas, no como una falla.
- **El horario.** `CONVERSATION_FOLLOWUP_SEND_HOURS` es obligatoria con el flag prendido y se lee en la zona del manifiesto (`instancia.zona_horaria`). Fuera de [inicio, fin), en la hora local, el barrido no lee el catalogo ni Chatwoot: queda `healthy`, con el resumen `outside_send_hours`, y no manda nada. `00-24` es a cualquier hora, escrito a proposito. Con la ventana por defecto (de 24 a 72 h) toda candidata cruza al menos un horario abierto. Lo que se pierde de noche: una plantilla que Meta pauso recien se ve en el primer barrido del horario.
- **El nombre.** Un saludo que no pasa `template_greeting_name_is_safe`, el filtro del despachador de ATT1 para sus plantillas, no sale: el nombre de perfil de WhatsApp puede ser un telefono, y sin primer nombre el saludo cae al nombre completo. El intento queda `greeting_name_refused` sin reservar nada, el barrido sigue `healthy` y el proximo lo vuelve a contar. El log lleva la conversacion y la fuente del saludo, nunca el nombre.
- **El modo de un solo telefono.** Con `CONVERSATION_FOLLOWUP_ONLY_PHONE` solo ese telefono recibe, comparado en forma canonica (`52...` y `521...` son el mismo movil). Las demas candidatas no se releen ni se reservan: se cuentan como `held_only_phone`, que el resumen muestra siempre, aunque no este entre los cuatro motivos mas frecuentes. Es una cota superior del ultimo barrido, no un acumulado: candidatas segun Chatwoot antes de la relectura, del filtro del nombre, del resolvedor y de toda barrera de la base (compra, derivacion, opt-out, conversacion no adoptada, uno por conversacion). No dice a cuantas personas les habria salido, y con un `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` de prueba cuenta a quien callo ese tiempo, no 24 h. Menos de 24 h de silencio (`CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` menor a `86400`) es solo para la prueba: con el flag prendido y sin `CONVERSATION_FOLLOWUP_ONLY_PHONE`, el bridge no arranca. Asi, abrir sacando solo el telefono no le manda el cupon a todo el inbox a los 15 minutos, dentro de la ventana de 24 h. Sin el modo de prueba, el alcance es el del entrante: todo el inbox con los remitentes por scope (ATT1), y solo `ALLOWED_WHATSAPP_JID` sin el.
- **El manifiesto es el techo.** El flag cuelga de `[flujos].descuento` ([ADR-0023](../decisions/0023-el-cupon-con-manifiesto-reusa-descuento.md)). `CONVERSATION_FOLLOWUP_TEMPLATE_NAME` y `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` tienen que nombrar la plantilla de `[plantillas].descuento` (sin ese lugar declarado no arranca), y `CONVERSATION_FOLLOWUP_PRODUCT_NAME` tiene que ser `hotmart.product_name`. Con el binding v1 (`COMMERCIAL_ALLY_CONFIG_PATH` sin `INSTANCE_MANIFEST_PATH`) no arranca: ahi no hay resolvedor ni reserva portable.

No cambian el barrido, la evaluacion contra Chatwoot, la relectura antes de reservar, los regimenes, el link y el boton, ni el ciclo de un envio. En ninguna instancia el seguimiento pasa por el piloto: ni por su pausa, ni por sus topes, ni por el tope de mensajes proactivos por persona, ni por `META_FINAL_EFFECT_ENABLED`. Se frena sacando el flag y redesplegando.

Lo prueban `tests/test_followup_discount.py` (el barredor con la conversacion 21 de ATT1 capturada y anonimizada, `chatwoot_followup_candidate_inbox_11_20261010.json`, contra su catalogo), `tests/test_supabase.py` (la ruta de cada reserva), `tests/test_followup_discount_wiring.py`, `tests/test_instance_wiring.py` y `tests/test_att1_production_settings.py` (el arranque y `/ready`; el ultimo, ademas, un barrido completo del barredor que arma el set de produccion con los clientes reales de Chatwoot y Supabase sobre `httpx.MockTransport`: la reserva va a `claim_portable_conversation_followup_v1` con la identidad resuelta), `tests/test_portable_conversation_followup_migration.py` (la migracion como texto) y `tests/sql/followup_engine/validate_portable_conversation_followup.mjs` (las RPC reales sobre la cadena de ATT1, en PGlite y como `service_role`).

## Configuracion

| Variable | Default | Nota |
|---|---|---|
| `CONVERSATION_FOLLOWUP_ENABLED` | `false` | Prendido sin lo siguiente, el bridge no arranca |
| `CONVERSATION_FOLLOWUP_TEMPLATE_NAME` | — | `johanna_seguimiento_descuento_01`. Con manifiesto, igual a `plantillas.descuento.nombre` (`att1_seguimiento_descuento_01` en ATT1) |
| `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` | — | `en` en Johanna: Meta registro asi `johanna_seguimiento_descuento_01`. Con manifiesto es obligatoria e igual a `plantillas.descuento.idioma` (`es_MX` en ATT1) |
| `CONVERSATION_FOLLOWUP_COUPON_CODE` | — | `[A-Za-z0-9_-]{1,64}`, creado en Hotmart para el producto |
| `CONVERSATION_FOLLOWUP_PRODUCT_NAME` | — | el `{{2}}` de la plantilla. Con manifiesto, igual a `hotmart.product_name` |
| `CONVERSATION_FOLLOWUP_INTERVAL_SECONDS` | `300` | |
| `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` | `86400` | 24 h. Con manifiesto y el flag prendido, un valor menor exige `CONVERSATION_FOLLOWUP_ONLY_PHONE`: es solo para la prueba |
| `CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS` | `259200` | 72 h |
| `CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN` | `10` | |
| `CONVERSATION_FOLLOWUP_MAX_PAGES` | `5` | Por estado (`open`, `resolved`), 25 conversaciones por pagina |
| `CONVERSATION_FOLLOWUP_SEND_HOURS` | — | `HH-HH`, con 0 <= inicio < fin <= 24, en la zona del manifiesto: `09-21`, o `00-24` para mandar a cualquier hora. Solo con manifiesto, y obligatoria con el flag prendido |
| `CONVERSATION_FOLLOWUP_ONLY_PHONE` | — | El modo de un solo telefono, en E.164 (`+` y de 7 a 15 digitos). Vacia: el alcance del entrante (todo el inbox con los remitentes por scope) |

Ademas exige el AgentBot, los IDs canonicos de Chatwoot, la admision de Corte B (el link sale del caso comercial que abre), Supabase y un alcance acotado de remitentes. Con manifiesto exige tambien lo de "En un runtime con manifiesto": el flujo `descuento` declarado, la plantilla y el producto del manifiesto, el horario y 24 h de silencio o mas, salvo en la prueba con el telefono.

El bridge no arranca en estos casos. Algunos frenan aunque el flag este apagado: un valor que no puede tener efecto no espera a que alguien prenda el seguimiento.

| Mensaje | Cuando | Con el flag apagado |
|---|---|---|
| `CONVERSATION_FOLLOWUP_SEND_HOURS must be HH-HH with 00 <= start < end <= 24 in the time zone of the instance manifest, ...` | El horario no tiene la forma `HH-HH` o esta fuera de rango (`21-09`, `09-25`, `00-00`): al leer la variable, y en el arranque si los `Settings` se arman a mano | Tambien |
| `invalid conversation followup configuration` | Un intervalo, una ventana, un tope o unas paginas invalidos | Tambien |
| `CONVERSATION_FOLLOWUP_ONLY_PHONE must be an E.164 phone` | El telefono de prueba no es E.164 | Tambien |
| `CONVERSATION_FOLLOWUP_SEND_HOURS requires an instance manifest: ...` | Hay horario y no hay manifiesto | Tambien |
| `CONVERSATION_FOLLOWUP_ENABLED in a portable runtime requires INSTANCE_MANIFEST_PATH` | El binding v1 en JSON | No |
| `runtime flags exceed the instance manifest flows: conversation_followup_enabled->descuento` | `[flujos].descuento = false` | No |
| `CONVERSATION_FOLLOWUP_TEMPLATE_NAME must match plantillas.descuento.nombre of the instance manifest` (y lo mismo para `_LANGUAGE` con `idioma` y `_PRODUCT_NAME` con `hotmart.product_name`) | Con manifiesto: otra plantilla o sin `[plantillas].descuento`, otro idioma o ninguno, u otro producto | No |
| `CONVERSATION_FOLLOWUP_SEND_HOURS is required with an instance manifest: ...` | Con manifiesto, sin horario | No |
| `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS below 86400 requires CONVERSATION_FOLLOWUP_ONLY_PHONE with an instance manifest: ...` | Con manifiesto, menos de 24 h de silencio sin el telefono de prueba (por ejemplo, abrir sacando solo `ONLY_PHONE`) | No |

`/ready` publica `conversation_followup` (estado del ultimo barrido) y `conversation_followup_last_scan` (solo conteos y motivos, sin datos de nadie). Con manifiesto suma, leidos del barredor que corre y sin ningun numero:

- `conversation_followup_audience`: `only_phone` en el modo de un solo telefono; sin el, `allowed_jid` si los remitentes van por `ALLOWED_WHATSAPP_JID` sin scope (el barredor solo mira ese numero, como el resto del entrante) e `inbox` con los remitentes por scope, que es todo el inbox;
- `conversation_followup_min_age_seconds`: cuanto tiene que llevar callado el lead (`"900"` en la prueba, `"86400"` abierto). Abrir se verifica por presencia con `inbox` y `86400`, no por la ausencia del modo de prueba;
- `conversation_followup_send_hours` (`09-21`).

El payload de Johanna no cambia. Un barredor caido no devuelve 503.

## Lo que queda afuera

- La revision diaria no clasifica todavia la marca `conversation_followup_command_key`: el mensaje aparece como plantilla generica.
- La plantilla con boton ya esta capturada del catalogo en las dos instancias (ver "La plantilla"), pero el barredor de los tests sigue usando por defecto la forma documentada por Meta (`tests/fixtures/meta_template_url_button_documented_20260928.json`).
- Que hace WhatsApp con el sufijo del boton al tocarlo (si conserva `?`, `&`, `~` y `%7C`) no esta medido: el primer envio salio y se leyo, pero ningun evento de Hotmart trajo todavia su `sck`.
- El seguimiento queda `sent` cuando Chatwoot acepta el mensaje. Si Meta lo rechaza despues (por ejemplo, por el tope de plantillas de marketing por persona), la fila sigue `sent` y el unico envio de esa conversacion queda gastado.
- No hay tope diario: el techo es uno por conversacion y `CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN` por barrido. Una persona con dos conversaciones puede recibir dos.
- El agente no sabe del cupon. Quien pide el link despues de recibirlo lo recibe a precio completo, salvo que use el boton o el codigo, y un pedido de descuento se deriva a una persona.
- En un runtime con manifiesto no corrio todavia contra Meta: falta el E2E con el modo de un solo telefono. `validate` no distingue cual de los dos flags que cuelgan de `descuento` habilita el flujo ([ADR-0023](../decisions/0023-el-cupon-con-manifiesto-reusa-descuento.md)).
