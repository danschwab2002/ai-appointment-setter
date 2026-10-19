# Referencia del manifiesto de instancia (`instancia.toml`, esquema `setter-instancia/v2`)

Para quien instala el setter en un negocio nuevo. El manifiesto describe **una instancia**: una aliada, con su número de WhatsApp, su producto y sus ofertas. Vive en la raíz del repo privado de la instancia. **Ningún campo es secreto:** tokens, hottok y claves van en las variables del servicio, nunca acá.

Se valida sin red ni secretos:

```sh
uv run python -m bridge.instance_cli validate <carpeta-de-la-instancia>
```

Sale con 0 si es válido y con 1 si no, y dice en castellano qué campo está mal y por qué. También lista qué flujos se pueden prender y qué le falta a cada uno, y si `[adaptadores.ghl]` tiene la aceptación del riesgo.

El formato es TOML: lo lee la biblioteca estándar de Python, sin dependencias, y no convierte en booleano un código de oferta como `no` u `off`. Un ejemplo completo y real es el manifiesto de ATT1 en `tests/fixtures/instances/att1/instancia.toml`.

## Nivel superior

| Campo | Qué es | Si está mal |
|---|---|---|
| `schema` | Siempre `"setter-instancia/v2"` | No carga |
| `producto` | Versión del setter que usa la instancia, `"v1.0.0"`. Es el tag de la imagen que se despliega | No carga |
| `eventos` | Qué hechos le llegan a esta instancia: `intencion` (formulario de la landing, directo por `/webhooks/lead` o por el adaptador de GHL), `carrito` (abandono en Hotmart), `pago_fallido`, `compra`, `entrante` (mensaje de WhatsApp), `ficha_impresa` (la ficha de pago en efectivo, `PURCHASE_BILLET_PRINTED`) | Un evento desconocido no carga. Un evento ausente deja apagados los flujos que dependen de él |

## `[instancia]`

| Campo | Qué es | Ejemplo |
|---|---|---|
| `tenant_ref` | La empresa que opera la instancia | `"lancemos"` |
| `ally_ref` | La aliada. Es la identidad de la instancia | `"att1"` |
| `funnel_ref` | El embudo dentro de la empresa. Junto con `tenant_ref` y `binding_version` forma la clave del binding en la base | `"att1"` |
| `binding_version` | Sube cuando cambia algo del binding que la base tiene que registrar | `1` |
| `marca` | Cómo se nombra a la aliada en los mensajes del sistema | `"Dra. Nina Garza"` |
| `zona_horaria` | Zona IANA de la aliada; ordena horarios y reportes | `"America/Mexico_City"` |

Los `*_ref` son slugs: minúsculas, números y guiones.

## `[hotmart]`

| Campo | Qué es |
|---|---|
| `product_id` | ID numérico del producto en Hotmart |
| `hotlink` | El código del checkout (`pay.hotmart.com/<hotlink>`) |
| `product_name` | Nombre del producto tal como lo nombra la aliada |
| `moneda` | Tres letras mayúsculas: `"USD"` |
| `precio` | Texto decimal (`"47"`) o entero. Un número con decimales de TOML (`47.0`) no se acepta, para no perder precisión |

### `[[hotmart.ofertas]]`, una por cada landing

| Campo | Qué es |
|---|---|
| `codigo` | El `off=` de Hotmart |
| `site` | Slug del sitio de la landing |
| `landing_id` | Slug de la landing dentro del sitio |
| `url` | `https://host/ruta` de la landing, sin query ni fragmento |
| `origen` | `pauta`, `organico` u `otro`. Es lo que después separa ventas de anuncios y orgánicas |
| `por_defecto` | Exactamente una oferta lleva `true`: es la del link cuando no se sabe qué landing vio la persona |

Reglas: al menos una oferta, códigos sin repetir y una sola oferta por landing. El link de pago se arma con la oferta de la landing que vio la persona. Una oferta que no está en esta lista nunca sale en un link.

## `[chatwoot]`

| Campo | Qué es |
|---|---|
| `account_id` | Cuenta de Chatwoot de la aliada |
| `inbox_id` | Inbox de WhatsApp de la aliada |
| `equipo_derivacion` | Opcional. Team de Chatwoot al que se derivan las conversaciones que necesitan una persona |

## `[inbound]` y `[consentimiento]`

| Campo | Qué es |
|---|---|
| `inbound.scope_key` / `scope_version` | El alcance con el que se admiten mensajes entrantes. Cambiar la versión obliga a registrar el alcance nuevo en la base |
| `consentimiento.copy_version` | Versión del texto de consentimiento de contacto por WhatsApp que ve la persona en el formulario |

## `[plantillas]`

Una entrada por cada plantilla de Meta, con `{ nombre = "...", idioma = "es_MX" }`. Los lugares posibles son `precheckout`, `carrito`, `pago_fallido`, `reactivacion`, `descuento` y `ficha`. El nombre y el idioma tienen que ser **exactamente** los aprobados en Meta: una plantilla aprobada en otro idioma es otra plantilla. Una plantilla que todavía no existe se omite, y el flujo que la usa no se puede prender.

Las plantillas de primer contacto (`precheckout`, `carrito` y `pago_fallido`) aceptan una clave opcional, `parametros`: las variables del cuerpo aprobado, en el orden en que la plantilla las numera.

```toml
carrito = { nombre = "att1_carrito_abandonado_01", idioma = "es_MX", parametros = ["nombre"] }
```

| Valor | Qué va en esa variable |
|---|---|
| `"nombre"` | El nombre con que se saluda a la persona: el saludo que calcula el envío si lo hay, o el nombre completo que trajo el evento |
| `"producto"` | El nombre del producto que guardó el caso de recuperación |

- Van una o dos variables, sin repetir. `["nombre"]` llena solo `{{1}}`; `["producto", "nombre"]` pone el producto en `{{1}}` y el nombre en `{{2}}`.
- Sin `parametros` vale `["nombre", "producto"]`, que es lo que el bridge manda hoy.
- La lista tiene que coincidir con el cuerpo aprobado en Meta: si la plantilla tiene una sola variable y se le mandan dos, Meta puede rechazar el envío. El bridge no lo verifica contra el catálogo al arrancar. Con `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true` el dispatcher lo verifica en cada envío: lee el catálogo del inbox y, si los marcadores del cuerpo no son exactamente los declarados, no manda y deja `approved_template_mismatch` ([approved-template-direct-dispatch-v1.md](contracts/approved-template-direct-dispatch-v1.md)).
- Si una variable declarada llega vacía (un carrito sin nombre), el envío se bloquea con `template_parameters_missing`. Una variable que la plantilla no declara no se exige.
- El bridge usa las de `carrito`, las de `pago_fallido` y, con `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`, las de `precheckout` (el primer contacto del formulario).
- `reactivacion` y `descuento` arman sus variables en su propio código: `parametros` ahí no carga. La de `descuento` es la plantilla del seguimiento con cupón (1.5.0): `{{1}}` es el nombre, `{{2}}` el producto y, si el texto también lleva el código, `{{3}}` el cupón. El cupón va siempre en el botón de URL, que es uno solo y apunta a `https://pay.hotmart.com/{{1}}`.

Con `carrito` o `pago_fallido` en `true` y la salida por WABA, el bridge no arranca si las variables del servicio no nombran la misma plantilla que el manifiesto: `WABA_FIRST_TOUCH_TEMPLATE_NAME` tiene que ser `carrito.nombre`, `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` tiene que ser `pago_fallido.nombre` (si está definida) y `WABA_TEMPLATE_LANGUAGE` tiene que ser el `idioma` de las dos. Por eso `carrito` y `pago_fallido` prendidos a la vez tienen que estar aprobadas en el mismo idioma. Con el flujo en `false`, sus `parametros` no se usan.

**`ficha` tiene su propio vocabulario**: `parametros` elige de `"nombre"`, `"producto"`, `"tienda"` (la de la fila de `[[ficha.medios]]`, por ejemplo OXXO) y `"vencimiento"` (la fecha en español, «jueves 22 de octubre»), de una a cuatro. Sin `parametros` vale `["nombre", "producto", "tienda", "vencimiento"]`, el orden de la plantilla aprobada de ATT1. Su botón de URL es el de la ficha y su base la da la fila (`boton_prefijo`). Con `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED`, `WABA_PAYMENT_SLIP_TEMPLATE_NAME` tiene que ser `ficha.nombre` ([portable-payment-slip-first-contact-v1.md](contracts/portable-payment-slip-first-contact-v1.md)).

Para `precheckout` vale la misma regla con `WABA_PRECHECKOUT_TEMPLATE_NAME`, que tiene que ser `precheckout.nombre` y compartir `WABA_TEMPLATE_LANGUAGE` y `WABA_TEMPLATE_CATEGORY` con las otras. No tiene préstamo: sin esa variable, el primer contacto del formulario no sale, y nunca usa la plantilla del carrito ([portable-precheckout-first-contact-v1.md](contracts/portable-precheckout-first-contact-v1.md)).

## `[agente]`

| Campo | Qué es |
|---|---|
| `modelo` | El nombre del modelo (profile de Hermes) que responde por esta instancia |
| `conocimiento` | Ruta, relativa a la carpeta de la instancia, del archivo de conocimiento (contrato en `docs/contracts/commercial-knowledge-v1.md`) |

## `[flujos]`

Los seis flujos, cada uno `true` o `false`: `inbound`, `precheckout`, `carrito`, `pago_fallido`, `reactivacion` y `descuento`. Es el estado **declarado** de la instancia. Un flujo en `true` exige su evento en `eventos` y su plantilla en `[plantillas]`; si falta alguno, el manifiesto no carga. `inbound = true` exige además el conocimiento aprobado.

| Flujo | Evento que lo dispara | Plantilla |
|---|---|---|
| `inbound` | `entrante` | — |
| `precheckout` | `intencion` | `precheckout` |
| `carrito` | `carrito` | `carrito` |
| `pago_fallido` | `pago_fallido` | `pago_fallido` |
| `reactivacion` | `entrante` | `reactivacion` |
| `descuento` | `entrante` | `descuento` |
| `ficha` (opcional) | `ficha_impresa` | `ficha` |

El flujo declarado es el techo: el mensaje sale recién cuando además está prendido el flag del servicio ([instance-runtime-v2.md](contracts/instance-runtime-v2.md)). Para `precheckout` ese flag es `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`: el envío del formulario con consentimiento planifica un único primer contacto, demorado, con la plantilla `precheckout`. Con `[adaptadores.ghl]` en el manifiesto, `precheckout` y `pago_fallido` en `true` exigen además la aceptación escrita del riesgo del adaptador (abajo): sin ella el bridge no arranca y `validate` da error.

**`descuento` es el seguimiento con cupón** (desde la 1.5.0, [ADR-0023](decisions/0023-el-cupon-con-manifiesto-reusa-descuento.md)). A quien contestó una plantilla nuestra, recibió la respuesta del agente y se quedó callado entre 24 y 72 h sin comprar, le manda una vez la plantilla de `[plantillas].descuento`, con el cupón en el botón. Su flag es `CONVERSATION_FOLLOWUP_ENABLED`, y con él el bridge exige:

- que `CONVERSATION_FOLLOWUP_TEMPLATE_NAME` y `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` nombren la plantilla de `[plantillas].descuento`, y que `CONVERSATION_FOLLOWUP_PRODUCT_NAME` sea `hotmart.product_name`;
- un horario en `CONVERSATION_FOLLOWUP_SEND_HOURS`, `HH-HH` en la hora de `instancia.zona_horaria` (`09-21`). `00-24` manda a cualquier hora, y hay que escribirlo;
- 24 h de silencio o más en `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS`, salvo en la prueba con `CONVERSATION_FOLLOWUP_ONLY_PHONE`.

El mismo flujo es el techo del descuento posterior a la respuesta (`CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED`), que nació con el binding v1: no corre junto al agente de Corte B y no manda mensajes, porque deja la acción diferida. `validate` informa el flujo, no cuál de los dos flags lo usa. Contrato: [conversation-followup-discount-v1.md](contracts/conversation-followup-discount-v1.md), sección *En un runtime con manifiesto*.

**`ficha` es opcional** (desde la ficha de pago, C1): un manifiesto que no la declara carga igual que antes y la ficha queda apagada. En `true` exige, además del evento y la plantilla, al menos una fila habilitada en `[[ficha.medios]]`, y su flag es `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED`. Usa la intención del formulario como permiso, igual que `precheckout` y `pago_fallido`, así que con `[adaptadores.ghl]` también exige la aceptación escrita del riesgo.

## `[[ficha.medios]]`, opcional

Qué medio de pago en qué país tiene una ficha a la que se le puede escribir. Una fila por medio y país; un país nuevo entra agregando una fila, sin tocar el código.

```toml
[[ficha.medios]]
medio = "CASHPAYMENT"
pais = "MX"
tienda = "OXXO"
zona_horaria = "America/Mexico_City"
referencia_largo = 34
vencimiento_digitos = [19, 26]
vencimiento_dias_si_falta = 3
boton_prefijo = "https://customer-local-latam.ebanx.com/print/oxxo?hash="
habilitada = true
```

| Campo | Qué es |
|---|---|
| `medio` y `pais` | El `payment.type` y el país del checkout de Hotmart que matchean la fila |
| `tienda` | Dónde se paga; va a la variable `"tienda"` |
| `zona_horaria` | La del país: con ella se calcula el día del vencimiento y si la ficha ya venció |
| `referencia_largo` y `vencimiento_digitos` | El largo de `billet_barcode` y los dígitos (desde 1, inclusive) que traen el vencimiento en `AAAAMMDD` |
| `vencimiento_dias_si_falta` | Si la referencia no trae la fecha, el vencimiento es el día de generada más estos días |
| `boton_prefijo` | La base del link de la ficha; tiene que coincidir con la del botón aprobado en Meta, y lo que sigue es el sufijo del botón |
| `habilitada` | `false` deja la fila escrita sin mandar nada |

Una ficha que no matchea ninguna fila habilitada queda `ignored` con `payment_slip_method_not_enabled`; una cuyo link no empieza con `boton_prefijo`, con `payment_slip_url_prefix_mismatch`; una ficha ya vencida al componer no se manda (`payment_slip_expired`). Una fila mal escrita no carga el manifiesto.

## `[guardas]`

| Campo | Qué es |
|---|---|
| `terminos_sensibles` | Raíces de palabras sensibles de la vertical (`"levotiroxin"` cubre levotiroxina) |
| `acciones_sensibles` | Raíces de acciones (`"dejar"`, `"dosis"`) |

Un mensaje que nombra un término junto con una acción se deriva a una persona sin pasar por el agente.

## `[slack]` y `[revision_diaria]`, opcionales

| Campo | Qué es |
|---|---|
| `slack.canal` | ID del canal de la aliada (`C...`) |
| `revision_diaria.revisores` | Quién revisa las conversaciones en la revisión diaria |

## `[adaptadores]`, opcional

Un adaptador traduce lo que manda una fuente externa a un evento canónico, que después entra por el mismo camino que el de una landing. Hoy hay uno: el del formulario de GHL, que convierte el webhook de un workflow de GHL en una `intencion` ([ghl-precheckout-adapter-v1.md](contracts/ghl-precheckout-adapter-v1.md)).

```toml
eventos = ["carrito", "pago_fallido", "compra", "entrante", "intencion"]

[adaptadores.ghl]
formularios = ["EgDqRl2xWc59YjVW1q8W", "Om5FpIg5Sr5ce7nSkuPy"]
# Opcionales, las tres o ninguna: la aceptación escrita del riesgo del adaptador.
riesgo_aceptado_por = "<COMPLETAR: nombre de quien decide>"
riesgo_aceptado_el = 2026-10-01
riesgo_contrato = "ghl-precheckout-adapter-v1"
# Opcionales, las dos o ninguna (1.4.0): en qué landing vive cada formulario.
landing_por_formulario = { Om5FpIg5Sr5ce7nSkuPy = "alimenta-tu-tiroides-d" }
landing_por_formulario_modo = "sombra"
```

El marcador entre `<` y `>` no carga: se reemplaza por el nombre de quien decide.

| Campo | Qué es | Si está mal |
|---|---|---|
| `adaptadores.ghl.formularios` | Los formularios de GHL cuyos envíos entran como `intencion`. Cada uno es el id que llega en `attributionSource.mediumId` del webhook: 20 letras o números | No carga |
| `adaptadores.ghl.riesgo_aceptado_por` | Opcional. Quién acepta el riesgo del adaptador: el responsable de la instancia. Texto no vacío, sin `<` ni `>` | No carga. El marcador del ejemplo tampoco |
| `adaptadores.ghl.riesgo_aceptado_el` | Opcional. Cuándo lo aceptó. Una fecha TOML sin comillas ni hora (`2026-10-01`), no posterior a hoy (un día de margen sobre la fecha UTC) | Un texto, una fecha con hora o una fecha futura no cargan |
| `adaptadores.ghl.riesgo_contrato` | Opcional. Contra qué contrato lo aceptó. Tiene que ser `"ghl-precheckout-adapter-v1"`, el vigente | Otro valor no carga: la aceptación es de otra versión del contrato |
| `adaptadores.ghl.landing_por_formulario` | Opcional (1.4.0), con el modo. En qué landing vive cada formulario: `{ <formulario> = "<landing_id>" }`. GHL manda la página donde empezó la visita, que no siempre es la del formulario ([contrato](contracts/ghl-precheckout-adapter-v1.md#la-landing-de-un-formulario-declarado-140)). Un formulario que vive en más de una página no se declara | No carga si un formulario no está en `formularios`, si el `landing_id` no es el de exactamente una oferta, o si la tabla está vacía |
| `adaptadores.ghl.landing_por_formulario_modo` | Opcional (1.4.0), con la tabla. `"sombra"`: la landing sigue saliendo de la URL y el log anota cuándo el formulario la habría cambiado. `"activo"`: la landing sale del formulario | Otro valor no carga |

Reglas: al menos un formulario, sin repetir, y `intencion` en `eventos`; si no, el manifiesto no carga. La landing y la oferta salen de la URL del envío, comparada con las `url` de `[[hotmart.ofertas]]`, salvo para un formulario declarado en `landing_por_formulario` con el modo `"activo"`. Sin la sección, o con `[adaptadores]` vacío, no hay adaptador.

**Listar un formulario es una afirmación.** Cada envío traducido se admite con `whatsapp_contact = true`, así que listar un formulario afirma que muestra la aclaración de `consentimiento.copy_version` y que su envío es el paso previo al checkout de la oferta de su landing. Se suma a la lista solo después de verificar las dos.

La sección sola no prende nada: el adaptador corre con `GHL_PRECHECKOUT_ADAPTER_ENABLED` y su token en las variables del servicio.

### La aceptación del riesgo

El token del adaptador es su única barrera y lo lee cualquier usuario de la subcuenta de GHL; una intención que entró por el adaptador no se distingue en la base de la de una landing (sección *Riesgos* del contrato). Para usar esas intenciones como permiso de contacto o como audiencia, quien decide por la instancia lo acepta por escrito con las tres claves `riesgo_*`.

- **Las tres o ninguna.** Con una o dos, el manifiesto no carga. No hay una clave de estado: la presencia completa es la aceptación.
- **Se escriben a mano.** Ningún código del producto las genera ni las completa. Van en el repo de la instancia, por PR, con el nombre y la fecha de quien decide.
- **Sin la aceptación, y con la sección en el manifiesto**, el bridge no arranca con `precheckout` ni `pago_fallido` en `true`, esté o no prendido `GHL_PRECHECKOUT_ADAPTER_ENABLED`. `validate` lo marca como error. Con la frontera del piloto prendida tampoco arranca si el scope del piloto es de audiencia `consented_intent` o `consented_intent_in_cohort`: eso vive en la base y `validate` no lo ve, lo avisa.
- **Con la aceptación**, esos flujos y esas audiencias se pueden prender. `validate` informa quién, cuándo y qué contrato; `/ready` informa la fecha y el contrato, nunca el nombre.
- **Quitar la sección no saca de la base las intenciones que el adaptador ya admitió.** Sin la sección el bridge deja de exigir la aceptación y las trata como las de una landing; `validate` lo avisa. La sección no se quita mientras haya intenciones vivas admitidas por el adaptador.
- **Orden al actualizar.** Un bridge anterior a 1.3.0 no carga un manifiesto con estas claves: primero la imagen nueva, después el manifiesto con la aceptación.

La guarda completa, con lo que responde `/ready` en cada caso, está en [ghl-precheckout-adapter-v1.md](contracts/ghl-precheckout-adapter-v1.md), *La aceptación escrita del riesgo*.

## `[[productos]]`, opcional

Los productos de la aliada. Atan cada producto con su sección del conocimiento y le ponen a cada conversación la etiqueta del producto en Chatwoot, para que el equipo vea en el inbox de qué producto es. Con un solo producto, lo que se vende sale de `[hotmart]`.

```toml
[[productos]]
clave = "att1"
etiqueta = "att1"
macro_etiqueta = 12
```

| Campo | Qué es | Si está mal |
|---|---|---|
| `clave` | La misma `clave` que la sección del producto en el conocimiento (`[[productos]]` del archivo de `agente.conocimiento`). Un slug en minúsculas | No carga |
| `etiqueta` | Opcional, con el macro. La etiqueta de Chatwoot del producto: minúsculas, números, guiones y guiones bajos. No puede ser `automation_paused` ni `automation_opted_out` | No carga |
| `macro_etiqueta` | Opcional, con la etiqueta. El id del macro de Chatwoot que suma esa etiqueta | No carga si no es un entero positivo. El bridge no arranca si es el macro de pausa, de reanudar o de baja |

**Varios productos (paso 2c).** El primero de la lista es el **principal**: lo suyo sale de `[hotmart]` y del `funnel_ref` y el `binding_version` de `[instancia]`, como siempre. Desde el segundo, cada producto es su propio binding en la base y trae su funnel y su Hotmart, con las mismas claves que `[hotmart]`:

```toml
[[productos]]
clave = "att1"            # el principal: sin funnel ni hotmart propios
etiqueta = "att1"
macro_etiqueta = 7

[[productos]]
clave = "att2"
funnel_ref = "att2"
binding_version = 1
etiqueta = "att2"
macro_etiqueta = 8

[productos.hotmart]
product_id = 8437428
hotlink = "…"
product_name = "…"
moneda = "USD"
precio = "67"

[[productos.hotmart.ofertas]]
codigo = "…"
site = "…"
landing_id = "…"
url = "https://…"
origen = "pauta"
por_defecto = true
```

| Campo | Qué es | Si está mal |
|---|---|---|
| `funnel_ref`, `binding_version` | Solo desde el segundo producto. El binding de ese producto en la base. El `funnel_ref` no puede repetirse entre productos (tampoco con el de `[instancia]`) | No carga |
| `[productos.hotmart]` | Solo desde el segundo producto. Lo mismo que `[hotmart]`, con sus ofertas | No carga |

Reglas:

- **Hasta 8 productos.** El principal y siete más. El principal no puede traer `funnel_ref`, `binding_version` ni `hotmart`: los suyos están arriba.
- **El ruteo tiene que poder separarlos.** No pueden repetirse entre productos: el par (producto de Hotmart, oferta), la landing (`site`/`landing_id`), la página (host y ruta de la `url`), la `clave`, la `etiqueta` ni el `macro_etiqueta`. Dos productos pueden compartir un producto de Hotmart si sus ofertas no se pisan (ATT2 se vendió con ofertas del producto de ATT1); en ese caso, el `hotlink` y el `product_name` tienen que ser los mismos, porque son los de Hotmart.
- **Con más de un producto, el bridge enruta todo por producto** (pasos 2c-2 a 2c-3b, [commercial-ally-runtime-v1](contracts/commercial-ally-runtime-v1.md)). Los eventos de Hotmart y los formularios van al binding de su producto; el link sale con la oferta y el hotlink del producto que nombra el agente; la etiqueta y lo que hizo la persona son de cada producto. Para que el link de un producto salga, el catálogo del scope entrante tiene que traer sus ofertas con su `funnel_ref` y una por defecto. Con uno, nada cambia.
- **Solo el principal tiene flujos de recuperación.** Los eventos y formularios de los demás productos se admiten en su funnel (la historia, la correlación y los frenos los ven), pero no se planifica nada para ellos: sus flujos llegan con el motor de flujos.
- **`/ready` verifica el binding de cada producto.** Con más de uno, publica también `commercial_ally_bindings` con cuántos son. Si falta uno, o no coincide con el manifiesto, responde 503 `commercial_ally_binding_unavailable`.
- **El conocimiento separa los mismos productos.** Con `[[productos]]`, el conocimiento tiene que tener una sección por producto con las mismas claves. Si no, `validate` lo marca como error y el bridge no arranca. Sin la sección no se ata nada, y el conocimiento puede tener una sección o varias.
- **La etiqueta va por un macro, nunca por el endpoint de etiquetas.** `POST /conversations/{id}/labels` reemplaza la lista entera y puede borrar `automation_paused`; la acción `add_label` de un macro solo suma. El macro se crea en la cuenta de la aliada, **global** y con **una sola acción**: `add_label` con la etiqueta del producto. Antes de usarlo, el bridge lo lee. Si hace cualquier otra cosa, o no existe, no lo ejecuta nunca y el etiquetado queda apagado hasta el próximo arranque, con `product_label_macro_invalid` en el log.
- **Cuándo se pone.** Cuando la persona escribe (antes de la admisión y de la pausa: la etiqueta dice de qué producto es la conversación, la atienda el agente o una persona) y cuando una plantilla de primer contacto sale. Las conversaciones que ya existían se etiquetan la próxima vez que la persona escribe.
- **Con más de un producto, cada uno con su etiqueta.** Cuando la persona escribe, la conversación lleva la etiqueta de cada producto que aparece en lo que hizo (formularios, carritos, pagos y compras de su funnel); sin nada, ninguna, porque no se adivina el producto. Cuando sale un link, la del producto del link. Con una plantilla, la del principal: hoy solo el principal tiene flujos. `/ready` publica `product_label_<clave>` por producto en vez de `product_label`.
- **Falla blanda.** Una etiqueta que no sale no frena la respuesta ni la plantilla: queda `product_label_failed` en el log y se reintenta en el próximo mensaje. `/ready` publica `product_label`: `unverified` (todavía no se leyó el macro), `ready` o `disabled:<motivo>`, sin responder 503.
- **Orden al actualizar.** Un bridge sin `[[productos]]` (hasta 1.9.0) no carga un manifiesto con esta sección: primero la imagen nueva, después el manifiesto.
- **Lo que ve el agente (2b).** Con `[[productos]]`, cada turno del agente lleva `products` (la clave y el nombre de cada producto) y `person_activity` (lo que hizo la persona en los últimos 30 días: formularios, carritos, pagos rechazados, fichas y compras, con su producto y su hora), y el agente dice en `product` de qué producto es el link que pide. El detalle está en [commercial-ally-runtime-v1](contracts/commercial-ally-runtime-v1.md), «Los productos y lo que hizo la persona». Hace falta la migración `20261016000200` aplicada en la base de la instancia: sin ella, el agente contesta igual, sin `person_activity`, y queda `person_activity_lookup_failed` en el log. El SOUL común tiene que ser el de la misma versión del producto: uno anterior no conoce `product` y pide el link sin él, lo que con un solo producto sigue funcionando.

## Relación con el binding v1

Mientras los caminos del bridge lean el binding de una aliada (`CommercialAllyConfig`, `docs/contracts/commercial-ally-runtime-v1.md`), el manifiesto v2 se traduce a él: la oferta por defecto es la del binding, y las demás landings van en `additional_offer_codes`, así un carrito o un pago fallido que entra por cualquier landing de la instancia se admite. El sitio, la landing y la URL de cada una van en `additional_offer_landings`, así también se admite el formulario del precheckout de cada landing (migración `20260930000200`); la fila del binding en la base tiene que declarar las mismas, o el bridge ve drift. El test `test_johanna_manifest_produces_the_binding_in_code_plus_its_other_landing_offers` prueba que el manifiesto de Johanna produce el binding que hoy está en el código, y que lo único que suma son sus otras cinco landings.
