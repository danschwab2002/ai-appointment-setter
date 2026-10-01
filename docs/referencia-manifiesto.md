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
| `eventos` | Qué hechos le llegan a esta instancia: `intencion` (formulario de la landing, directo por `/webhooks/lead` o por el adaptador de GHL), `carrito` (abandono en Hotmart), `pago_fallido`, `compra`, `entrante` (mensaje de WhatsApp) | Un evento desconocido no carga. Un evento ausente deja apagados los flujos que dependen de él |

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

Una entrada por cada plantilla de Meta, con `{ nombre = "...", idioma = "es_MX" }`. Los lugares posibles son `precheckout`, `carrito`, `pago_fallido`, `reactivacion` y `descuento`. El nombre y el idioma tienen que ser **exactamente** los aprobados en Meta: una plantilla aprobada en otro idioma es otra plantilla. Una plantilla que todavía no existe se omite, y el flujo que la usa no se puede prender.

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
- `reactivacion` y `descuento` arman sus variables en su propio código: `parametros` ahí no carga.

Con `carrito` o `pago_fallido` en `true` y la salida por WABA, el bridge no arranca si las variables del servicio no nombran la misma plantilla que el manifiesto: `WABA_FIRST_TOUCH_TEMPLATE_NAME` tiene que ser `carrito.nombre`, `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` tiene que ser `pago_fallido.nombre` (si está definida) y `WABA_TEMPLATE_LANGUAGE` tiene que ser el `idioma` de las dos. Por eso `carrito` y `pago_fallido` prendidos a la vez tienen que estar aprobadas en el mismo idioma. Con el flujo en `false`, sus `parametros` no se usan.

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

El flujo declarado es el techo: el mensaje sale recién cuando además está prendido el flag del servicio ([instance-runtime-v2.md](contracts/instance-runtime-v2.md)). Para `precheckout` ese flag es `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`: el envío del formulario con consentimiento planifica un único primer contacto, demorado, con la plantilla `precheckout`. Con `[adaptadores.ghl]` en el manifiesto, `precheckout` y `pago_fallido` en `true` exigen además la aceptación escrita del riesgo del adaptador (abajo): sin ella el bridge no arranca y `validate` da error.

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
formularios = ["EgDqRl2xWc59YjVW1q8W"]
# Opcionales, las tres o ninguna: la aceptación escrita del riesgo del adaptador.
riesgo_aceptado_por = "<COMPLETAR: nombre de quien decide>"
riesgo_aceptado_el = 2026-10-01
riesgo_contrato = "ghl-precheckout-adapter-v1"
```

El marcador entre `<` y `>` no carga: se reemplaza por el nombre de quien decide.

| Campo | Qué es | Si está mal |
|---|---|---|
| `adaptadores.ghl.formularios` | Los formularios de GHL cuyos envíos entran como `intencion`. Cada uno es el id que llega en `attributionSource.mediumId` del webhook: 20 letras o números | No carga |
| `adaptadores.ghl.riesgo_aceptado_por` | Opcional. Quién acepta el riesgo del adaptador: el responsable de la instancia. Texto no vacío, sin `<` ni `>` | No carga. El marcador del ejemplo tampoco |
| `adaptadores.ghl.riesgo_aceptado_el` | Opcional. Cuándo lo aceptó. Una fecha TOML sin comillas ni hora (`2026-10-01`), no posterior a hoy (un día de margen sobre la fecha UTC) | Un texto, una fecha con hora o una fecha futura no cargan |
| `adaptadores.ghl.riesgo_contrato` | Opcional. Contra qué contrato lo aceptó. Tiene que ser `"ghl-precheckout-adapter-v1"`, el vigente | Otro valor no carga: la aceptación es de otra versión del contrato |

Reglas: al menos un formulario, sin repetir, y `intencion` en `eventos`; si no, el manifiesto no carga. La landing y la oferta no se declaran acá: salen de la URL del envío, comparada con las `url` de `[[hotmart.ofertas]]`. Sin la sección, o con `[adaptadores]` vacío, no hay adaptador.

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

## Relación con el binding v1

Mientras los caminos del bridge lean el binding de una aliada (`CommercialAllyConfig`, `docs/contracts/commercial-ally-runtime-v1.md`), el manifiesto v2 se traduce a él: la oferta por defecto es la del binding, y las demás landings van en `additional_offer_codes`, así un carrito o un pago fallido que entra por cualquier landing de la instancia se admite. El sitio, la landing y la URL de cada una van en `additional_offer_landings`, así también se admite el formulario del precheckout de cada landing (migración `20260930000200`); la fila del binding en la base tiene que declarar las mismas, o el bridge ve drift. El test `test_johanna_manifest_produces_the_binding_in_code_plus_its_other_landing_offers` prueba que el manifiesto de Johanna produce el binding que hoy está en el código, y que lo único que suma son sus otras cinco landings.
