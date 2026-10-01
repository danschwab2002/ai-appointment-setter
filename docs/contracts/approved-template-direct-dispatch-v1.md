# Contrato: el dispatcher manda la plantilla aprobada sin borrador de Hermes (v1)

- **Estado:** construido; apagado por defecto; sin E2E real.
- **Fecha:** 2026-09-30
- **Alcance:** el primer contacto del carrito, del pago fallido y, desde el bridge 1.3.0, del formulario de la landing, que sale por el dispatcher durable de una instancia con manifiesto v2 y salida por WABA.
- **Relacionados:** [instance-runtime-v2.md](instance-runtime-v2.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [referencia-manifiesto.md](../referencia-manifiesto.md#plantillas), [lead-first-name-inference-v1.md](lead-first-name-inference-v1.md), [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md).

## Por qué existe

Hasta esta versión el dispatcher le pedía a Hermes un borrador antes de cada envío (`request_followup_message`). En un primer contacto por WABA eso no sirve por tres razones:

1. **El texto de Hermes no llega a Meta.** Meta muestra el cuerpo aprobado de la plantilla con sus variables. El borrador terminaba solo en el `content` que guarda Chatwoot y en el hash del gate final, o sea que lo que se auditaba no era lo que veía la persona.
2. **El agente no puede escribir primero.** El SOUL común (`profiles/agente-comercial-comun/SOUL.md`) le prohíbe el contacto proactivo, así que el pedido de borrador de un primer contacto choca con sus propias reglas.
3. **El largo.** El validador de un borrador corta en 500 caracteres; un cuerpo aprobado puede llegar a 1024 (el límite de Meta).

En el modo directo el dispatcher arma el texto con el cuerpo aprobado del catálogo que Chatwoot publica para el inbox y no llama a Hermes.

## Configuración

| Variable | Por defecto | Qué hace |
|---|---|---|
| `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED` | `false` | Prende el modo directo del dispatcher |
| `LEAD_FIRST_NAME_GREETING_ENABLED` | `false` | Con el modo directo, la variable `nombre` lleva el saludo por primer nombre. En un runtime portable se admite solo con el modo directo: sin él el bridge no arranca |
| `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY` | vacía | Solo con manifiesto. La categoría de Meta de la plantilla del pago fallido cuando no es la de `WABA_TEMPLATE_CATEGORY` (`MARKETING` o `UTILITY`). Vacía, todas las plantillas usan `WABA_TEMPLATE_CATEGORY`. Exige `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` |
| `WABA_PRECHECKOUT_TEMPLATE_NAME` | vacía | Solo con manifiesto. La plantilla del primer contacto del formulario; tiene que ser `plantillas.precheckout.nombre`. Entra a la configuración del dispatcher solo con `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`. Usa `WABA_TEMPLATE_CATEGORY` y `WABA_TEMPLATE_LANGUAGE` |

Con `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true` el bridge no arranca si falta alguna de estas condiciones:

- `INSTANCE_MANIFEST_PATH` (un manifiesto v2);
- `DURABLE_OUTBOUND_ENABLED=true`, que ya exige la frontera del piloto;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER=waba` y las variables `WABA_*` de la plantilla;
- un flujo portable de salida prendido (`PORTABLE_HOTMART_RECOVERY_ENABLED`, `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` o `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`), que es lo que le da al dispatcher el binding de la instancia. El primer contacto del formulario alcanza solo, sin carrito ni pago fallido.

El dispatcher, además, rechaza al construirse un cliente de Hermes, la admisión de derivación, o que le falten las plantillas, el inbox de Chatwoot o el sender.

Con el modo directo prendido:

- Hermes deja de ser una dependencia del dispatcher: `HERMES_API_BASE_URL` y `HERMES_API_KEY` no hacen falta para la salida. `HERMES_MODEL_NAME` se sigue exigiendo porque el manifiesto lo compara con `agente.modelo`.
- El dispatcher no cuenta como consumidor de `HUMAN_HANDOFF_ADMISSION_ENABLED`: nunca recibe una sugerencia de derivar. Si la admisión está prendida, la tiene que consumir el Corte B.
- `LEAD_FIRST_NAME_GREETING_ENABLED` llega al dispatcher. En un runtime portable el dispatcher directo es el único que lo usa (los one-shots de Johanna, el otro consumidor, no son portables), así que con el flag prendido y el modo directo apagado el bridge no arranca: `LEAD_FIRST_NAME_GREETING_ENABLED in a portable runtime requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`. Antes se aceptaba y no saludaba a nadie. Sin manifiesto (Johanna) nada cambia.

**Guarda nueva sobre el gate final.** Con manifiesto, `META_FINAL_EFFECT_ENABLED=true` junto con `DURABLE_OUTBOUND_ENABLED=true` exige el modo directo. Sin él, lo que se autorizaría es un borrador de Hermes que Meta no muestra.

**Qué corta el efecto final de ATT1 con el manifiesto v2.** El candado de `create_app` que rechaza `META_FINAL_EFFECT_ENABLED` para ATT1 (`commercial_ally_config.tenant_ref == "att1"`, del 2026-09-04) es del binding v1 (`COMMERCIAL_ALLY_CONFIG_PATH`) y **no dispara con el manifiesto v2**: ahí `tenant_ref` es `"lancemos"` y `ally_ref` es `"att1"`. No hay que contar con él. Para ATT1 v2 lo que impide que un mensaje llegue a Meta es:

- en el bridge, `META_FINAL_EFFECT_ENABLED=false` (el único corte del efecto final; con él en `true`, la guarda de arriba solo exige que el texto sea el de la plantilla aprobada);
- en la base, el estado del piloto (`pilot_runtime_controls` en `inactive` hasta armarlo), la cohorte y los topes del scope, que `mark_*_request_started` revisa antes de cada envío.

El candado viejo no se cambió a `ally_ref`: dejaría a ATT1 v2 sin poder abrir nunca el efecto final. `test_final_meta_effect_with_the_direct_mode_builds` (`tests/test_instance_wiring.py`) fija que con el manifiesto v2 no dispara, y `tests/test_att1_final_meta_gate.py` que sigue disparando con el binding v1.

## Qué hace el dispatcher con una acción vencida

La reevaluación, la reserva del intento, el chequeo del destinatario, la segunda reevaluación, el gate final, `request_started`, el envío y la aceptación son los mismos del modo con Hermes. Lo que cambia es de dónde sale el texto. Para una acción de ancla `precheckout_intent`, la reevaluación y `request_started` son las RPC propias de ese flujo ([portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md)); el bridge las elige por el `anchor_type`.

1. **Solo `first_contact_review`.** Cualquier otra acción (un seguimiento `no_reply_review`) se cierra con `approved_template_direct_unsupported_action`, sin consultar a nadie y sin reintento.
2. **La plantilla** se elige por el ancla de la acción. `payment_failure`: la de `pago_fallido` si hay una propia, si no la de `carrito`. `cart_abandonment`: la de `carrito`. `precheckout_intent`: la de `precheckout`, y **sin préstamo**: si no está configurada, el intento se cierra con `first_touch_template_not_configured`, sin reintento, antes de leer el catálogo, del gate final y de `request_started`. El sender repite ese corte antes de tocar Chatwoot. Un primer contacto del formulario nunca sale con la plantilla del carrito.
3. **Las variables** salen de `parametros` de esa plantilla en el manifiesto ([referencia-manifiesto.md](../referencia-manifiesto.md#plantillas)). Si el runtime no las tiene declaradas (el flujo está en `false`), el intento se cierra con `approved_template_mismatch`: el dispatcher no adivina el cuerpo. Si una variable declarada llega vacía (sin nombre o sin producto), se cierra con `template_parameters_missing` antes de leer el catálogo. Cada valor sale con los espacios colapsados (`WhatsAppTemplateConfig.body_values`): Meta rechaza (132018) un parámetro con salto de línea, tabulación o más de cuatro espacios seguidos, y lo rechaza después de empezado el pedido. El texto hasheado y `processed_params` salen de esos mismos valores; el contacto de Chatwoot conserva el nombre tal como llegó.
4. **El saludo.** Con `LEAD_FIRST_NAME_GREETING_ENABLED=true`, `nombre` es `resolve_greeting_name(nombre completo)`: la inferencia `confident` guardada si hay, si no el primer nombre determinístico, si no el nombre completo. El contacto de Chatwoot se sigue creando con el nombre completo.
   - **El nombre completo sale solo si pasa un filtro** (desde el bridge 1.3.0). El valor que va a `nombre`, con saludo o sin él, pasa por `template_greeting_name_is_safe` siempre que la plantilla declare `nombre`. El nombre llega del formulario público o de Hotmart con un teléfono que nadie verificó, y el nombre completo es el último recurso del saludo (sin saludo, es el valor mismo): sin el filtro, una URL, un email o un texto largo salían dentro de la plantilla aprobada.
   - El filtro admite letras, marcas, emoji, espacios y `'’-.,`, hasta 60 caracteres con los espacios colapsados. Rechaza dígitos, `/`, `@`, `:`, `_`, los caracteres de dirección de texto (U+202E y familia) y la forma de un dominio: un punto pegado a dos letras (`evil.example`; `J.C. Pérez` pasa).
   - Si no pasa, el intento se cierra con `template_parameters_missing`, sin reintento, antes de leer el catálogo y sin tocar Chatwoot. El warning `approved_template_name_refused` lleva la acción, el ancla y de qué nivel salió el valor (`inferred`, `deterministic`, `full_name` o `buyer_name` sin saludo), nunca el nombre.
   - El costo medido: los 19 patrones capturados del inbox 9 pasan, con saludo y sin saludo, incluidos una sola letra, iniciales con emoji y solo emoji. Un nombre real de más de 60 caracteres sin saludo, o con forma de dominio (`Ma.José`), no sale.
   - `resolve_greeting_name` no cambia: el filtro vive solo en el modo directo.
5. **El catálogo** se lee en cada envío (`GET /api/v1/accounts/{cuenta}/inboxes/{inbox}`) con el token de control, igual que la reactivación. Una plantilla que Meta pausa o rechaza corta el envío en vez de producir mensajes rechazados.
6. **El texto** es el cuerpo aprobado con `{{1}}` a `{{n}}` reemplazados en una sola pasada. Es lo que va al hash del gate final (`content_sha256`), al `content` del mensaje de Chatwoot y a `record_and_finalize_followup_acceptance`.
7. **El envío** pasa a `send_first_touch` el mismo saludo como `greeting_name`, así que la variable que recibe Meta y el texto que se hasheó salen del mismo valor.

La propuesta interna queda como `strategy = "approved_template:<nombre>"`. No se guarda en ningún lado; sirve para los logs.

### El destinatario

Desde el bridge 1.3.0, en un runtime con destinatario dinámico (un flujo portable de salida prendido) el dispatcher resuelve a quién le va a escribir **antes** de la reevaluación final, del gate final y de `request_started`. El mismo móvil puede estar en Chatwoot con dos formas (`52` + 10 dígitos o `521` + 10 en México; `54` + 10 o `549` + 10 en Argentina).

1. **Busca las dos formas** en Chatwoot, solo con lecturas (`resolve_first_touch_recipient`).
   - Existe un contacto: es el destinatario, con su `source_id`.
   - Existen los dos: el de la forma de entrega (`521…` en México, `54…` en Argentina), que es donde Chatwoot resuelve la respuesta: no normaliza México y a un entrante `549…` le busca primero el contacto `54…`. Queda un warning con el inbox, la región y los ids de Chatwoot, nunca el número.
   - No existe ninguno: el destinatario es la forma de entrega, y el contacto se crea con ella al mandar.
2. **La forma de entrega** la decide una sola función, `whatsapp_delivery_phone`: México como `521` + 10, Argentina como `54` + 10, cualquier otro número igual a sí mismo. Sale de una medición del 2026-10-01 sobre el Chatwoot de producción (versión 4.13), en otro inbox; el detalle está en [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md#teléfonos-las-dos-formas-del-mismo-móvil). No prueba el envío en ATT1: ahí se confirma en el E2E.
3. **El gate final recibe ese `wa_id`.** `target_phone` del efecto final, y su hash en la evidencia, son los del número que Meta va a recibir, no los del teléfono como lo guarda el contacto.
4. **Si no es el teléfono consentido, no sale.** Cuando el `wa_id` resuelto no es canónicamente el teléfono del caso, el intento se cierra con `chatwoot_recipient_phone_mismatch`, sin reintento.
5. **Si Chatwoot falla** en esa búsqueda, el intento se cierra con `pre_request_failed` y el lote sigue. La búsqueda es anterior a `request_started`.
6. **El envío no vuelve a buscar.** `send_first_touch` recibe el destinatario ya resuelto. Si el contacto recién creado queda con otro `source_id` que el esperado, no manda (`contact_inbox_source_mismatch`).

Sin manifiesto (Johanna) nada de esto corre: el sender y el dispatcher nacen con la equivalencia apagada y el destinatario es el de siempre.

### Qué exige del catálogo

La plantilla se busca por nombre **y** idioma, porque Meta admite el mismo nombre en varios idiomas.

| Chequeo | `reason_code` del intento | Detalle en el log |
|---|---|---|
| El inbox no responde, o la respuesta no trae `message_templates` | `approved_template_unavailable` | `invalid_inbox_payload` o el tipo de error HTTP |
| No hay ninguna plantilla con ese nombre | `approved_template_unavailable` | `not_found` |
| La plantilla no está `APPROVED` | `approved_template_unavailable` | `not_approved` |
| Está con ese nombre pero en otro idioma | `approved_template_mismatch` | `language_mismatch` |
| Hay dos con el mismo nombre e idioma | `approved_template_mismatch` | `ambiguous_template` |
| La categoría no es la de esa plantilla: `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY` para la del pago fallido si está definida, si no `WABA_TEMPLATE_CATEGORY` | `approved_template_mismatch` | `category_mismatch` |
| No hay exactamente un `BODY` con texto | `approved_template_mismatch` | `invalid_body` |
| Los marcadores no son exactamente `{{1}}` a `{{n}}`, con `n` = las variables declaradas (un marcador con nombre también cuenta) | `approved_template_mismatch` | `unexpected_placeholders` |
| Un botón que no es `QUICK_REPLY` | `approved_template_mismatch` | `unsupported_button` |
| Un `HEADER` que no es de texto, o un `HEADER`/`FOOTER` con variables | `approved_template_mismatch` | `component_requires_parameters` |
| Otro tipo de componente | `approved_template_mismatch` | `unsupported_component` |
| El texto renderizado pasa de 1024 caracteres | `approved_template_mismatch` | `rendered_body_too_long` |
| Un valor vacío al renderizar | `template_parameters_missing` | `empty_parameter` |
| Un valor con salto de línea, tabulación o más de cuatro espacios seguidos al renderizar (uno que no pasó por `body_values`) | `template_parameters_missing` | `invalid_parameter_whitespace` |

Hay tres cierres que no dependen del catálogo: `first_touch_template_not_configured` (el primer contacto del formulario sin su plantilla, antes de leerlo), `template_parameters_missing` por un nombre que no pasa el filtro del paso 4 (también antes de leerlo) y `chatwoot_recipient_phone_mismatch` (el destinatario resuelto no es el teléfono consentido, después de armar el texto y antes del gate final).

Todos cierran el intento con `failed_before_request`: no se marcó `request_started` ni salió ningún POST. El `reason_code` es texto libre en la base, así que no hace falta migración.

### Cuándo se reintenta

- **Lo que puede arreglarse solo se reintenta:** el catálogo que no responde, una plantilla que no está, está pausada o no cierra con idioma, categoría, marcadores o botones (`approved_template_unavailable`, `approved_template_mismatch` del catálogo). El dispatcher pide otro intento al minuto; la base lo concede mientras la acción tenga reintentos (`max_execution_retries`, 3 por defecto: cuatro intentos en total) y no haya vencido, y después la cierra como `permanent_failed` con el último motivo. Cada reintento vuelve a leer el catálogo.
- **Lo que no cambia solo cierra la acción en el primer intento** como `permanent_failed`, sin pedir otro: la acción que no es primer contacto (`approved_template_direct_unsupported_action`), las variables que el runtime no declara (`approved_template_mismatch` sin `parametros`), un valor del caso que falta, que Meta rechaza o un nombre que no pasa el filtro (`template_parameters_missing`), el primer contacto del formulario sin su plantilla (`first_touch_template_not_configured`) y un destinatario que no es el teléfono consentido (`chatwoot_recipient_phone_mismatch`). Reintentarlos solo volvía a reclamar la acción y a leer el catálogo tres veces más para llegar al mismo cierre.

## Qué no cambia

- **Johanna:** corre sin manifiesto, con el dispatcher apagado y sin el flag. El modo directo exige manifiesto, así que no se le puede prender, y sus one-shots no pasan por el dispatcher. El filtro del nombre (paso 4) tampoco la toca: vive en el modo directo, y `resolve_greeting_name`, que usan sus one-shots, no cambió.
- **El modo con Hermes:** con el flag apagado el dispatcher hace exactamente lo de antes, incluida la validación de 500 caracteres y la derivación.
- `reactivation.py` y `followup_discount.py` no se tocaron: el parser nuevo sigue su patrón.

## Lo que no está medido

- **El catálogo del inbox 11 de ATT1 está capturado, no su respuesta por la API.** `tests/fixtures/chatwoot_inbox_11_message_templates_20261001.json` es la columna `message_templates` del canal, leída el 2026-10-01: `att1_carrito_abandonado_01`, `att1_compra_fallida_01` y `att1_interes_precheckout_01` tienen dos variables, idioma `es_MX`, categoría `MARKETING` y tres botones `QUICK_REPLY`, así que con esa captura ninguna necesita `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY`. La cuarta, `att1_descuento_10_post_respuesta_01`, figura en idioma `en` y no cierra con el idioma de la instancia. De `GET /inboxes/11` no hay captura: los tests envuelven la lista como `{"message_templates": …}`. El catálogo puede cambiar después de la captura (Meta pausa o recategoriza una plantilla): si el real no cierra con `parametros`, cada acción termina en `permanent_failed` con `approved_template_mismatch` después de cuatro intentos, uno por minuto.
- **La forma de entrega en ATT1.** La regla de `whatsapp_delivery_phone` sale de una medición del 2026-10-01 sobre otro inbox del mismo Chatwoot. Ningún envío de ATT1 la confirma todavía, y no hay ningún contacto `549` medido.
- **Botones `QUICK_REPLY` con solo parámetros de cuerpo.** Las plantillas de Johanna `johanna_carrito_abandonado_01` y `johanna_compra_fallida_01` tienen tres `QUICK_REPLY` en el catálogo del 28/09 y los one-shots de Johanna las mandan con solo `processed_params.body`. No verifiqué que esos envíos sean posteriores a la carga de los botones. La prueba real es el primer envío de ATT1.
- **El token de control del inbox 11:** el `GET` del inbox lo hace el cliente de control; tiene que tener acceso a ese inbox.
- **`/ready` no revisa el catálogo.** Un catálogo que no cierra se ve recién en el intento, y cada acción vencida gasta sus cuatro intentos antes de cerrarse. Queda como deuda operativa: un chequeo del catálogo en `/ready` (leer el inbox y validar las plantillas de los flujos prendidos) lo mostraría antes del primer envío. Mientras no exista, se valida el catálogo a mano contra la captura del inbox antes de prender el flag.

## Pruebas

- `tests/test_approved_templates.py`: el parser contra los dos catálogos capturados del inbox 9 (23/09 y 28/09) y el del inbox 11 (2026-10-01), cada rama de rechazo y el render.
- `tests/test_durable_dispatcher_approved_template.py`: el dispatcher con un `ChatwootClient` real sobre un emulador y el `ChatwootMessageSender` real. Cubre las tres ofertas de ATT1 sin Hermes, el pago fallido con su plantilla, el saludo con los nombres capturados (determinístico, inferido y nombre completo), las tres plantillas de primer contacto del catálogo capturado del inbox 11 (una por ancla), el primer contacto del formulario sin plantilla, el destinatario resuelto antes del gate y el que no es el teléfono consentido, el hash del gate cerrado, una plantilla de una sola variable de punta a punta (`johanna_reactivacion_01` del 23/09: el cuerpo, el hash del gate y `processed_params` llevan solo `{"1"}`), cada plantilla contra su categoría, los catálogos que no cierran (ningún POST), los valores faltantes y los que Meta rechaza, el nombre que no es un nombre (URL, email, emoji con dominio, 61 caracteres y dígitos, con saludo y sin él: ningún pedido a Chatwoot) junto con los 19 patrones capturados que siguen saliendo por el ancla del formulario y la plantilla que no declara `nombre`, la acción que no es primer contacto y el dispatcher armado por `create_app` sin llamar a Hermes.
- `tests/test_instance_wiring.py`: los gates de arranque.
- Entre las dos capas: el mismo archivo corre el dispatcher directo con el `SupabaseClient` real sobre un emulador de PostgREST y compara el modo de la reserva, la RPC de arranque y la RPC de reevaluación de cada `anchor_type` (`cart_abandonment`, `payment_failure` y `precheckout_intent`) con lo que declara `tests/sql/followup_engine/validate_att1_portable_chain.mjs` (`DIRECT_DELIVERY_MODE` y las tablas `START_OPERATION_BY_ANCHOR` y `REEVALUATE_OPERATION_BY_ANCHOR`), que a su vez elige cada RPC con el `anchor_type` que escribió el planificador. **Deuda (D10):** ninguna prueba corre el dispatcher contra PostgREST y Postgres reales; si una capa cambia sin que la otra lo declare, las dos siguen en verde.
