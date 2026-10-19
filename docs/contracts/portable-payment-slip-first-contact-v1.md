# Contrato: primer contacto portable tras la ficha de pago en efectivo (v1)

- **Estado:** implementado en el árbol (rama `feat/claude-att1-ficha-efectivo-v1`), sin desplegar ni activar. Están las funciones puras de `src/bridge/payment_slip.py`, el parser de `src/bridge/hotmart.py`, la migración `20261019000100`, el armado de C1 en `src/bridge/payment_slip_first_touch.py` y su cableado en el worker, el dispatcher y el arranque del bridge, con sus pruebas.
- **Fecha:** 2026-10-18
- **Alcance:** una instancia con manifiesto v2 y salida por WABA. Un `PURCHASE_BILLET_PRINTED` de Hotmart cuyo medio y país están habilitados en la tabla `[[ficha.medios]]` del manifiesto planifica **un** primer contacto, demorado, que el dispatcher durable manda con la plantilla aprobada de la ficha (en ATT1, `att1_ficha_efectivo_01`).
- **Relacionados:** [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md) (el otro primer contacto portable), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md), [instance-runtime-v2.md](instance-runtime-v2.md). La admisión que ya guarda la ficha tal como llega es `admit_portable_hotmart_payment_slip_event_v1` (migración `20261012000100_portable_payment_failure_context_and_slip_events.sql`).

## Propósito y límite

Quien elige pagar en efectivo recibe de Hotmart una ficha con un link y una fecha de vencimiento. Si no la paga antes de esa fecha, la venta se pierde sin que nadie le haya escrito. Este contrato suma ese disparador, con un solo mensaje: la ficha a mano, la tienda donde se paga y hasta cuándo.

- Un `PURCHASE_BILLET_PRINTED` admitido y cruzado con la intención de compra de la persona planifica la acción de primer contacto sobre un caso de disparador `payment_slip`.
- Sale después de la demora de la política del scope (en ATT1, 60 minutos), dentro del horario de la política.
- Lo frenan, igual que en los otros flujos del piloto, la compra, la baja, la derivación a una persona y la pérdida del consentimiento.
- Con una política de un solo paso (`payment_slip_first_contact`) no manda seguimientos. Los recordatorios del vencimiento (C2 y C3) son pasos siguientes de la política: [portable-payment-slip-reminders-v1.md](portable-payment-slip-reminders-v1.md).

## El disparador

- Evento `PURCHASE_BILLET_PRINTED`, versión `2.0.0`, con `data.purchase.status = 'BILLET_PRINTED'`.
- Del producto del binding (`hotmart_product_id`) y con una oferta del binding (`offer_code` o `additional_offer_codes`).
- Con transacción `HP…` y email o teléfono de la persona, normalizados con las mismas reglas que el pago fallido (`checkout_phone` primero, después `phone`).

Lo implementa `parse_hotmart_payment_slip_payload(payload, *, config)` en `src/bridge/hotmart.py`, que devuelve `HotmartPaymentSlipData` o `None`. La proyección del comprador para la resolución de identidad es `parse_hotmart_payment_slip_buyer_payload`, que además exige el nombre del producto del binding (como el pago fallido) y lee el país de `data.purchase.checkout_country`, que es donde llega en las fichas capturadas.

**El webhook admite y cruza la intención de toda ficha del binding**, sea del medio que sea. **El filtro por medio y país va en el worker**, al componer: lo que no pasa queda `ignored` con uno de los motivos de abajo. Así la base conserva todas las fichas, también las de medios que hoy no se habilitan.

## La tabla `[[ficha.medios]]`

Vive en el manifiesto de la instancia (`instancia.toml`), no en variables de entorno. Una fila por medio de pago y país:

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

| Clave | Tipo | Regla |
|---|---|---|
| `medio` | texto | El `data.purchase.payment.type` de Hotmart, en mayúsculas (`[A-Z][A-Z0-9_]{1,63}`) |
| `pais` | texto | ISO 3166-1 alfa-2 en mayúsculas |
| `tienda` | texto | El nombre que va en la plantilla. No vacío, sin espacios en los bordes, imprimible, hasta 64 caracteres |
| `zona_horaria` | texto | Una zona IANA que `zoneinfo` conozca |
| `referencia_largo` | entero | El largo exacto de `billet_barcode` que se considera una referencia con vencimiento, de 1 a 128 |
| `vencimiento_digitos` | 2 enteros | Posiciones 1-based e inclusivas del vencimiento `AAAAMMDD` en la referencia: abarcan exactamente 8 dígitos y caen dentro de `referencia_largo` |
| `vencimiento_dias_si_falta` | entero | Días que se suman a la fecha de generada cuando la referencia no sirve, de 0 a 31 |
| `boton_prefijo` | texto | La parte fija del botón URL aprobado en Meta: `https://`, con host, terminada en `=` o `/`, sin espacios, `{`, `}` ni `#` |
| `habilitada` | booleano | Una fila deshabilitada no habilita su medio |

Las claves son exactamente esas nueve. Un medio y país repetido es un error. Una lista vacía es válida y no habilita nada. La validación es `parse_payment_slip_methods(raw)` en `src/bridge/payment_slip.py`: devuelve `tuple[PaymentSlipMethod, ...]` o levanta `ValueError` con el número de fila. La va a usar `instance_manifest.py` al cargar el manifiesto, así que una fila mal escrita impide arrancar.

## Cómo se resuelve una ficha

`payment_slip_facts(payload)` lee los hechos de la ficha y nunca levanta por un campo ausente o de otro tipo (lo deja en `None`):

| Hecho | De dónde |
|---|---|
| `payment_type` | `data.purchase.payment.type` |
| `checkout_country_iso` | `data.purchase.checkout_country.iso`; si falta, `data.buyer.address.country_iso`. En mayúsculas |
| `billet_url` | `data.purchase.payment.billet_url` |
| `billet_barcode` | `data.purchase.payment.billet_barcode` |
| `generated_at` | `data.purchase.order_date` (epoch en milisegundos), como fecha y hora UTC |

`resolve_payment_slip(facts, methods)` chequea en este orden y devuelve el primer motivo que corresponda, o la resolución:

| Orden | Motivo | Cuándo |
|---|---|---|
| 1 | `payment_slip_method_not_enabled` | Ninguna fila habilitada tiene ese `medio` y ese `pais` |
| 2 | `payment_slip_url_prefix_mismatch` | `billet_url` falta o no empieza con el `boton_prefijo` de la fila |
| 3 | `payment_slip_button_suffix_invalid` | Lo que sigue al prefijo no cumple `^[A-Za-z0-9_-]{8,128}$` |
| 4 | `payment_slip_due_date_unknown` | La referencia no sirve y tampoco hay `generated_at` |

El sufijo va como variable de un botón URL: el patrón deja afuera todo lo que cambiaría la URL aprobada (`&`, `#`, `/`, `%`, espacios). No se fija en 48 caracteres para no atar el contrato a un proveedor, aunque el hash medido de OXXO tiene siempre 48 hexadecimales en minúscula.

La resolución (`PaymentSlipResolution`) trae la fila elegida, `tienda`, `due_date`, `due_date_source` (`reference` o `generated_plus_days`), `button_suffix` y `button_url_template` (`boton_prefijo` + `{{1}}`).

### El vencimiento

1. **De la referencia**, si `billet_barcode` tiene exactamente `referencia_largo` dígitos ASCII y el tramo `vencimiento_digitos` forma una fecha válida que cae entre el día de generada (en `zona_horaria`) y 31 días después. Fuente: `reference`.
2. **Si no**, el día de `generated_at` en `zona_horaria` más `vencimiento_dias_si_falta`. Fuente: `generated_plus_days`.
3. **Si tampoco hay `generated_at`**, el motivo `payment_slip_due_date_unknown`.

**La evidencia (2026-10-18):** el fixture `tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json` trae los 13 `PURCHASE_BILLET_PRINTED` guardados en la base de ATT1 desde el 12/10, anonimizados. En las 5 fichas `CASHPAYMENT` de México, `billet_barcode` tiene 34 dígitos y los dígitos 19 a 26 son `AAAAMMDD` igual al día de `order_date` en `America/Mexico_City` más 3: 15/10, 16/10, 17/10, 19/10 y 20/10 de 2026. La de Chile trae una referencia de 1 carácter y un link con otra base (`https://api.ebanx.com/ws/redirect?hash=`). Los otros 7 (4 `MERCADO_PAGO`, 2 `NEQUI`, 1 `YAPE`) no traen `billet_url` ni `billet_barcode`. Lo prueba `tests/test_payment_slip.py`.

La ventana de 31 días del paso 1 es una salvaguarda: una referencia que diera una fecha anterior a la generada, o meses después, mostraría a la persona una fecha falsa. Ahí manda el respaldo.

## La plantilla

La plantilla de la ficha es de categoría `UTILITY`, con parámetros **posicionales**: 4 variables en el cuerpo y dos botones, **primero el URL** y después la respuesta rápida «No más mensajes». La forma está en `tests/fixtures/chatwoot_message_templates_inbox_11_ficha_20261018.json` (catálogo del inbox 11 de ATT1, 2026-10-18).

| Variable | Valor |
|---|---|
| `{{1}}` | El nombre de la persona, con la misma regla de saludo que las otras plantillas del armado |
| `{{2}}` | El nombre del producto del binding |
| `{{3}}` | `tienda` de la fila |
| `{{4}}` | `format_due_date_es(due_date)`: día de la semana, número y mes en minúscula, con tablas fijas y sin locale («jueves 22 de octubre») |
| Botón 1 (URL) | `button_suffix`. La URL del botón aprobado tiene que ser exactamente `button_url_template` |

En `processed_params` de Chatwoot: `body` con las claves `"1"` a `"4"` y `buttons` con un único `{"type": "url", "parameter": <button_suffix>}`, que corresponde al primer botón. Si la URL aprobada del botón no coincide con `button_url_template` de la fila, el arranque tiene que fallar: un sufijo pegado a otra base abriría otra página.

## Lo que se planifica alrededor (resumen del diseño)

Esto queda fijado acá como interfaz; el detalle lo implementan las migraciones y el cableado:

- **La base guarda los hechos crudos** en una tabla inmutable nueva (`commercial_ally_payment_slip_details`): la correlación, la intención, el medio, el país, `billet_url`, `billet_barcode` y `generated_at`. Tienda, vencimiento y sufijo **no** se guardan: se calculan en Python al componer con este módulo, para no duplicar la regla en SQL.
- **Las funciones del pago fallido no cambian.** Las de la ficha se derivan de su definición viva (`plan_payment_slip_recovery`, `plan_payment_slip_recovery_with_identity`, `plan_portable_payment_slip_recovery`, `mark_portable_payment_slip_request_started`) y la admisión con cruce es `admit_portable_hotmart_payment_slip_v2`. Una segunda ficha de la misma persona no adelanta la hora del contacto.
- **Un scope propio** (en ATT1, `att1-ficha`) con `source_event_type = 'PURCHASE_BILLET_PRINTED'`.
- **Configuración:** `PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED` (apagado por defecto), `LANCEMOS_PILOT_PAYMENT_SLIP_SCOPE_KEY` / `_VERSION`, `PAYMENT_SLIP_FOLLOWUP_POLICY_KEY` / `_VERSION` y `WABA_PAYMENT_SLIP_TEMPLATE_NAME` / `_CATEGORY`. En el manifiesto, el evento `ficha_impresa`, la ranura de plantilla `ficha` (hasta 4 parámetros) y el flujo `ficha`, **opcional**, para que un manifiesto sin él siga cargando.
- **El disparador `payment_slip` entra en los disparadores del primer toque sin préstamo:** sin eso, un disparador desconocido saldría con la plantilla del carrito.

## Al componer C1 (decididos por Dan el 2026-10-18)

- **La política vence al día** (decisión 4): si en un día no se pudo mandar C1 (fuera de horario, el tope, un freno), la acción expira sin mandarlo. Lo publica la instancia en su política (`expires_after`), no el bridge.
- **Una ficha ya vencida no se manda** (decisión 5): si al componer la fecha de vencimiento ya pasó en `zona_horaria` de la fila, el intento cierra sin reintento con `payment_slip_expired`. El día del vencimiento todavía se manda («hasta el jueves 22»): cuenta solo un día posterior.
- **La resolución se repite al componer.** El worker de resolución ya dejó `ignored` con su motivo (`payment_slip_method_not_enabled`, `_url_prefix_mismatch`, `_button_suffix_invalid`, `_due_date_unknown`) toda ficha que no resuelve, sin planificarla. Si igual llega una acción cuya ficha no resuelve (el manifiesto cambió en el medio), el intento cierra sin reintento con el mismo motivo, sin mensaje.
- **El botón se exige al componer, no al arrancar:** el bridge no lee el catálogo de Chatwoot al arrancar. `parse_approved_template(..., url_button=button_url_template)` exige que el primer botón de la plantilla aprobada sea exactamente `boton_prefijo` + `{{1}}`; si no, el intento cierra con `approved_template_mismatch` y se reintenta (el catálogo lo puede corregir Meta), sin mandar nada.
- Sin la configuración de la ficha en el dispatcher (el flag apagado), una acción `payment_slip` cierra con `first_touch_template_not_configured` antes de leer el catálogo: nunca sale con la plantilla del carrito.
- Los hechos crudos se leen con `get_portable_payment_slip_send_context` bajo el lease vigente. Si la base no contesta, `payment_slip_context_unavailable` y se reintenta; un 55000 (sin lease, una acción que no es de la ficha, un caso sin ficha resuelta) cierra con su motivo, sin reintento.

## Qué NO hace

- **No manda seguimientos** de la ficha (C2 a C5 de los flujos de ATT1): un solo mensaje.
- **No habilita otros países ni medios.** Chile, Mercado Pago, Nequi y Yape quedan `payment_slip_method_not_enabled` hasta que una fila del manifiesto los habilite con su prefijo medido.
- **No reprocesa** las fichas que llegaron antes de prender el flujo.
- **No recalcula** el vencimiento desde `PURCHASE_EXPIRED`.
- **No toca el carrito:** el planificador del carrito puede encontrarse con un caso abierto de la ficha del mismo contacto, producto y oferta; falla cerrado y solo con el flag prendido: el planificador del piloto del carrito aborta con `55000 pilot_case_binding_conflict` (medido en PGlite, `validate_portable_payment_slip_recovery.mjs`, sección 9), la transacción se deshace entera y el worker deja el carrito `failed` con ese motivo, sin excepción y sin mensaje (`tests/test_payment_slip_wiring.py`). Ese carrito no recibe su mensaje mientras el caso de la ficha siga abierto. Arreglarlo es tocar ese planificador y queda fuera de este contrato.
