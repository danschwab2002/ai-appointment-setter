# Contrato: el dispatcher manda la plantilla aprobada sin borrador de Hermes (v1)

- **Estado:** construido; apagado por defecto; sin E2E real.
- **Fecha:** 2026-09-30
- **Alcance:** el primer contacto del carrito y del pago fallido que sale por el dispatcher durable de una instancia con manifiesto v2 y salida por WABA.
- **Relacionados:** [instance-runtime-v2.md](instance-runtime-v2.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [referencia-manifiesto.md](../referencia-manifiesto.md#plantillas), [lead-first-name-inference-v1.md](lead-first-name-inference-v1.md).

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

Con `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED=true` el bridge no arranca si falta alguna de estas condiciones:

- `INSTANCE_MANIFEST_PATH` (un manifiesto v2);
- `DURABLE_OUTBOUND_ENABLED=true`, que ya exige la frontera del piloto;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER=waba` y las variables `WABA_*` de la plantilla;
- un flujo portable de recuperación prendido (`PORTABLE_HOTMART_RECOVERY_ENABLED` o `PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED`), que es lo que le da al dispatcher el binding de la instancia.

El dispatcher, además, rechaza al construirse un cliente de Hermes, la admisión de derivación, o que le falten las plantillas, el inbox de Chatwoot o el sender.

Con el modo directo prendido:

- Hermes deja de ser una dependencia del dispatcher: `HERMES_API_BASE_URL` y `HERMES_API_KEY` no hacen falta para la salida. `HERMES_MODEL_NAME` se sigue exigiendo porque el manifiesto lo compara con `agente.modelo`.
- El dispatcher no cuenta como consumidor de `HUMAN_HANDOFF_ADMISSION_ENABLED`: nunca recibe una sugerencia de derivar. Si la admisión está prendida, la tiene que consumir el Corte B.
- `LEAD_FIRST_NAME_GREETING_ENABLED` llega al dispatcher. En un runtime portable el dispatcher directo es el único que lo usa (los one-shots de Johanna, el otro consumidor, no son portables), así que con el flag prendido y el modo directo apagado el bridge no arranca: `LEAD_FIRST_NAME_GREETING_ENABLED in a portable runtime requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`. Antes se aceptaba y no saludaba a nadie. Sin manifiesto (Johanna) nada cambia.

**Guarda nueva sobre el gate final.** Con manifiesto, `META_FINAL_EFFECT_ENABLED=true` junto con `DURABLE_OUTBOUND_ENABLED=true` exige el modo directo. Sin él, lo que se autorizaría es un borrador de Hermes que Meta no muestra. El candado `tenant_ref == "att1"` del binding v1 no cambia; con manifiesto v2 no se activaba, porque ahí `tenant_ref` es `"lancemos"`.

## Qué hace el dispatcher con una acción vencida

La reevaluación, la reserva del intento, el chequeo del destinatario, la segunda reevaluación, el gate final, `request_started`, el envío y la aceptación son los mismos del modo con Hermes. Lo que cambia es de dónde sale el texto.

1. **Solo `first_contact_review`.** Cualquier otra acción (un seguimiento `no_reply_review`) se cierra con `approved_template_direct_unsupported_action`, sin consultar a nadie y sin reintento.
2. **La plantilla** se elige como hasta ahora: la de `pago_fallido` si la acción viene de un pago fallido y hay una propia, si no la de `carrito`.
3. **Las variables** salen de `parametros` de esa plantilla en el manifiesto ([referencia-manifiesto.md](../referencia-manifiesto.md#plantillas)). Si el runtime no las tiene declaradas (el flujo está en `false`), el intento se cierra con `approved_template_mismatch`: el dispatcher no adivina el cuerpo. Si una variable declarada llega vacía (sin nombre o sin producto), se cierra con `template_parameters_missing` antes de leer el catálogo. Cada valor sale con los espacios colapsados (`WhatsAppTemplateConfig.body_values`): Meta rechaza (132018) un parámetro con salto de línea, tabulación o más de cuatro espacios seguidos, y lo rechaza después de empezado el pedido. El texto hasheado y `processed_params` salen de esos mismos valores; el contacto de Chatwoot conserva el nombre tal como llegó.
4. **El saludo.** Con `LEAD_FIRST_NAME_GREETING_ENABLED=true`, `nombre` es `resolve_greeting_name(nombre completo)`: la inferencia `confident` guardada si hay, si no el primer nombre determinístico, si no el nombre completo. El contacto de Chatwoot se sigue creando con el nombre completo.
5. **El catálogo** se lee en cada envío (`GET /api/v1/accounts/{cuenta}/inboxes/{inbox}`) con el token de control, igual que la reactivación. Una plantilla que Meta pausa o rechaza corta el envío en vez de producir mensajes rechazados.
6. **El texto** es el cuerpo aprobado con `{{1}}` a `{{n}}` reemplazados en una sola pasada. Es lo que va al hash del gate final (`content_sha256`), al `content` del mensaje de Chatwoot y a `record_and_finalize_followup_acceptance`.
7. **El envío** pasa a `send_first_touch` el mismo saludo como `greeting_name`, así que la variable que recibe Meta y el texto que se hasheó salen del mismo valor.

La propuesta interna queda como `strategy = "approved_template:<nombre>"`. No se guarda en ningún lado; sirve para los logs.

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

Todos cierran el intento con `failed_before_request`: no se marcó `request_started` ni salió ningún POST. El `reason_code` es texto libre en la base, así que no hace falta migración.

### Cuándo se reintenta

- **Lo que puede arreglarse solo se reintenta:** el catálogo que no responde, una plantilla que no está, está pausada o no cierra con idioma, categoría, marcadores o botones (`approved_template_unavailable`, `approved_template_mismatch` del catálogo). El dispatcher pide otro intento al minuto; la base lo concede mientras la acción tenga reintentos (`max_execution_retries`, 3 por defecto: cuatro intentos en total) y no haya vencido, y después la cierra como `permanent_failed` con el último motivo. Cada reintento vuelve a leer el catálogo.
- **Lo que no cambia solo cierra la acción en el primer intento** como `permanent_failed`, sin pedir otro: la acción que no es primer contacto (`approved_template_direct_unsupported_action`), las variables que el runtime no declara (`approved_template_mismatch` sin `parametros`) y un valor del caso que falta o que Meta rechaza (`template_parameters_missing`). Reintentarlos solo volvía a reclamar la acción y a leer el catálogo tres veces más para llegar al mismo cierre.

## Qué no cambia

- **Johanna:** corre sin manifiesto, con el dispatcher apagado y sin el flag. El modo directo exige manifiesto, así que no se le puede prender, y sus one-shots no pasan por el dispatcher.
- **El modo con Hermes:** con el flag apagado el dispatcher hace exactamente lo de antes, incluida la validación de 500 caracteres y la derivación.
- `reactivation.py` y `followup_discount.py` no se tocaron: el parser nuevo sigue su patrón.

## Lo que no está medido

- **El catálogo del inbox 11 de ATT1** (paso A0 del plan). Sin esa captura no se sabe qué variables, categoría y botones tienen `att1_carrito_abandonado_01` y `att1_compra_fallida_01`. Los tests usan el catálogo capturado del inbox 9 del 28/09. Antes de prender el flag, la categoría de cada plantilla se lee en esa captura: si la del pago fallido no es la del carrito, va en `WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY`. Si el catálogo real no cierra con `parametros`, cada acción termina en `permanent_failed` con `approved_template_mismatch` después de cuatro intentos, uno por minuto.
- **Botones `QUICK_REPLY` con solo parámetros de cuerpo.** Las plantillas de Johanna `johanna_carrito_abandonado_01` y `johanna_compra_fallida_01` tienen tres `QUICK_REPLY` en el catálogo del 28/09 y los one-shots de Johanna las mandan con solo `processed_params.body`. No verifiqué que esos envíos sean posteriores a la carga de los botones. La prueba real es el primer envío de ATT1.
- **El token de control del inbox 11:** el `GET` del inbox lo hace el cliente de control; tiene que tener acceso a ese inbox.
- **`/ready` no revisa el catálogo.** Un catálogo que no cierra se ve recién en el intento, y cada acción vencida gasta sus cuatro intentos antes de cerrarse. Queda como deuda operativa: un chequeo del catálogo en `/ready` (leer el inbox y validar las plantillas de los flujos prendidos) lo mostraría antes del primer envío. Mientras no exista, se valida el catálogo a mano contra la captura del inbox antes de prender el flag.

## Pruebas

- `tests/test_approved_templates.py`: el parser contra los dos catálogos capturados del inbox 9 (23/09 y 28/09), cada rama de rechazo y el render.
- `tests/test_durable_dispatcher_approved_template.py`: el dispatcher con un `ChatwootClient` real sobre un emulador y el `ChatwootMessageSender` real. Cubre las tres ofertas de ATT1 sin Hermes, el pago fallido con su plantilla, el saludo con los nombres capturados (determinístico, inferido y nombre completo), el hash del gate cerrado, los catálogos que no cierran (ningún POST), los valores faltantes, la acción que no es primer contacto y el dispatcher armado por `create_app` sin llamar a Hermes.
- `tests/test_instance_wiring.py`: los gates de arranque.
