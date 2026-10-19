# Contrato: B1, el primer toque del pago rechazado en cuatro variantes (v1)

- **Estado:** propuesto e implementado en el árbol (rama `feat/claude-att1-pago-rechazado-b1-v1`), sin desplegar ni activar. Están la migración `20261019000200`, el módulo `src/bridge/payment_failure_variant.py`, su rama en el dispatcher, el scope y la política propios en el worker de resolución, `/ready` y el arranque del bridge, con sus pruebas.
- **Fecha:** 2026-10-19
- **Alcance:** una instancia con manifiesto v2, el pago fallido portable prendido (`PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED`) y salida por WABA en modo directo. Un `PURCHASE_CANCELED` de Hotmart que es un rechazo planifica **un** primer toque, 15 minutos después del último rechazo, con la plantilla de la primera variante que corresponde.
- **Relacionados:** [portable-payment-slip-first-contact-v1.md](portable-payment-slip-first-contact-v1.md) (el mismo molde de scope y política propios), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md). El cupón y el link de recuperación son los del carrito (migraciones `20261018000100` y `20261018000200`).

## Propósito y límite

Hasta la 1.11, el pago fallido de ATT1 escribe en el minuto 0, con una sola plantilla sin link ni cupón. Si hay dos rechazos seguidos, manda dos mensajes. Y trata como rechazo a un pago pendiente que se canceló. Medido sobre los 17 `PURCHASE_CANCELED` de ATT1 del 11 al 17/10 (`tests/fixtures/hotmart_purchase_canceled_att1_20261019.json`): 4 de las 10 personas con rechazo compraron solas en menos de 13 minutos.

- Sale **un** mensaje por persona y producto, el del último rechazo, a la gracia de la política contada desde ese rechazo (en ATT1, 15 minutos).
- Las cuatro variantes y el link con o sin cupón.
- Los toques 2 y 3 (B2 y Z) **no** están en este contrato. Usan el mecanismo de varios toques que se construye para A2 y Z del carrito.

## El disparador

- `PURCHASE_CANCELED` admitido y correlacionado como hasta hoy (`admit_portable_hotmart_payment_failure`, sin cambios).
- **Un cancelado que no es un rechazo no se planifica:** sin `refusal_reason` y con un medio que no es `CREDIT_CARD` ni `PAYPAL` (Mercado Pago, transferencia, Nequi). El planificador portable lo rechaza con `payment_failure_not_a_refusal` (55000) y el evento queda `failed` con ese motivo. Es el flujo 2 (el pendiente cancelado).

## El tipo de rechazo

`payment_failure_refusal_kind(refusal_reason, payment_type)` es la única regla, en la base. `refusal_reason` es texto libre y llega en dos idiomas:

| Resultado | Cuándo |
|---|---|
| `not_a_refusal` | motivo vacío y medio que no es tarjeta ni PayPal |
| `insufficient_funds` | el motivo, sin acentos y en minúscula, contiene `saldo`, `insufficient`, `fondos insuficientes`, `sin fondos` o `limite` |
| `declined` | todo lo demás: `Transaction refused`, `Transação negada pelo banco emissor` y cualquier texto que no se reconozca |

Sobre los 17 capturados: 9 `declined`, 3 `insufficient_funds` y 5 `not_a_refusal`.

## Un caso por persona y producto

`plan_payment_failure_recovery` cambia en tres cosas (parche en el lugar, con reemplazos contados):

- **La búsqueda del caso** deja de mirar la oferta y suma los casos que ya mandaron su mensaje, compraron o tomó una persona (`sequence_exhausted`, `won`, `escalated`), creados en las últimas **72 h**. Un rechazo que cae ahí se suma al caso: no abre otro ni manda otro mensaje. 72 h es lo que dura la secuencia entera (B1, B2 a las 24 h, Z 48 h después).
- **El segundo rechazo con el mensaje pendiente** deja `due_at` en `greatest(due_at, rechazo + gracia)` (antes, `least(due_at, now())`). Un `next_attempt_at` ya puesto se corre igual.
- **El contexto del caso** cuenta los rechazos: `payment_failure_count` (1 al crear, +1 por cada rechazo que se suma).

## El scope y la política propios

El planificador exige que la política del caso sea la del scope (`pilot_policy_mismatch`). Por eso el pago rechazado tiene los suyos, como la ficha:

| Variable | ATT1 |
|---|---|
| `LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_KEY` / `_VERSION` | `att1-pago-rechazado` / 1 (`source_event_type = PURCHASE_CANCELED`) |
| `PAYMENT_FAILURE_FOLLOWUP_POLICY_KEY` / `_VERSION` | `att1-pago-rechazado-un-toque` / 1 (gracia 15 min, un paso `payment_failure_first_contact`) |

- Se leen solo con `PAYMENT_FAILURE_VARIANTS_ENABLED`, como las de la ficha con su flag. Apagado, el bridge arranca igual con las constantes cargadas y el pago fallido se planifica con el scope y la política del carrito, como hasta la 1.11. Prendido, van los cuatro juntos.
- **El orden para prender:** aplicar la migración, publicar el scope y la política, armar el scope y recién después prender el flag. Con el flag prendido y el scope sin armar, cada rechazo se rechaza al planificar (`pilot_runtime_not_armed`) y no se reintenta.
- `/ready` publica `portable_payment_failure_own_scope` con el estado del runtime del scope, que lee `get_portable_payment_failure_pilot_runtime_status`. Si el scope no está publicado, o no acepta `PURCHASE_CANCELED`, o tiene otra versión activa, responde 503.
- Un caso abierto con el scope del carrito antes del cambio no se mezcla con el nuevo. Un rechazo que se suma a ese caso falla con `pilot_case_binding_conflict` y no sale mensaje. Es una sola vez, en la transición.

## Las variantes

`PAYMENT_FAILURE_VARIANTS_ENABLED` prende B1 entero: el scope y la política propios, y las variantes. Exige el scope propio, el modo directo (`DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED`), el worker del cupón (`RECOVERY_COUPON_WORKER_ENABLED`) y las cuatro plantillas:

| Variante | Sale si | Plantilla de ATT1 (variable) | Categoría | Cupón |
|---|---|---|---|---|
| `coupon` | la persona tiene un cupón `valid` de otro caso del producto, que vence en más de 1 h | `att1_pago_rechazado_cupon_01` (`WABA_PAYMENT_FAILURE_TEMPLATE_COUPON`) | MARKETING | ese, con su vencimiento |
| `two_refusals` | `payment_failure_count >= 2` | `att1_pago_rechazado_dos_veces_01` (`…_TWO_REFUSALS`) | UTILITY | no |
| `insufficient_funds` | el último rechazo es `insufficient_funds` | `att1_pago_rechazado_saldo_01` (`…_INSUFFICIENT_FUNDS`) | MARKETING | el propio, de 24 h |
| `declined` | el resto | `att1_pago_rechazado_banco_01` (`…_DECLINED`) | UTILITY | no |

- Sale la primera que corresponde, en ese orden.
- Variables, en el orden aprobado: `nombre`, `cuando` (la hora del último rechazo en la zona del país del teléfono), `producto` y, en `coupon` e `insufficient_funds`, `vence`.
- **El botón** es el URL de la plantilla con nuestro link de recuperación (`reserve_recovery_checkout_issuance_v1`, ahora también para `anchor_type = payment_failure`), más `&offDiscount=<código>` cuando la variante lleva cupón.
- **El cupón propio** lo siembra `claim_due_recovery_coupons_v1` cuando `payment_failure_wants_first_coupon(caso)` es verdadero: un solo rechazo, que es `insufficient_funds`, y sin cupón vigente de otro caso. Vence a `due_at + 24 h`.
- **Lo que necesita el dispatcher** lo da `get_portable_payment_failure_send_context_v1` bajo la lease vigente: `refusal_kind`, `payment_failure_count`, `latest_failed_at`, `payment_type` y el cupón vigente de otro caso. Un 55000 cierra el intento sin reintento.

**B1 nunca se pierde por el cupón o el link.**
- Si el cupón propio todavía no está `valid`, el envío espera 5 minutos en los intentos 1 y 2, dentro de los 15 minutos de su hora. Igual que A1.
- Si el cupón no llega a tiempo o está muerto, si el link no se puede emitir (`missing_offer`, `blocked_identity`, …) o si la plantilla no cierra, sale la plantilla de hoy (`WABA_PAYMENT_FAILURE_TEMPLATE_NAME`).
- La reserva del link frena B1 sin mandar nada cuando hay una compra conocida, una baja o un caso cerrado.

## El primer contacto del formulario

No cambia código. `_portable_precheckout_stop_reason` ya devuelve `superseded_by_provider_event` en dos casos: cuando hay un caso de Hotmart abierto del producto y cuando la intención quedó `payment_failure_supported`. Lo fija el validador, con el caso abierto y después de mandado el mensaje.

## Lo que se prueba

- `tests/sql/followup_engine/validate_portable_payment_failure_variants.mjs`, sobre la cadena real:
  - la migración agrega exactamente cuatro funciones y cambia solo las cuatro que parcha, cada una con sus reemplazos y nada más;
  - el tipo de rechazo de los 17 capturados;
  - el plan con la gracia, los dos rechazos en un minuto, el cancelado que no es un rechazo y el rechazo de otra oferta;
  - el primer contacto frenado, la siembra del cupón y B1 de punta a punta con el link;
  - el rechazo después del mensaje (se suma) y 73 h después (caso nuevo);
  - el cupón de otro caso.
- `tests/fixtures/flow_engine/legacy_recovery_cases.json` (G11 y G12) registra el comportamiento nuevo del legado.
- `tests/test_payment_failure_b1_dispatch.py`: las cuatro variantes por el dispatcher con el catálogo capturado del 19/10 (`chatwoot_message_templates_inbox_11_pago_rechazado_20261019.json`), la espera del cupón, la plantilla de hoy, el freno por compra y el contexto rechazado.
- `tests/test_payment_failure_variant.py`: la elección sobre los 17 capturados, las variables y el botón, la configuración, el worker con el scope propio, el arranque, `/ready` y `from_env`.
