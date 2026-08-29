# First-touch diferido desde precheckout — Johanna V1

- **Estado:** Macro completa implementada, revisada y verificada localmente; promoción, activación y aprobación Meta pendientes
- **Fecha:** 2026-08-29
- **Alcance:** `lead.precheckout` V1.1.0 autorizado sin señal Hotmart observada durante 60 minutos
- **No implementa:** publicación del scope, activación del flag, template Meta, deploy ni envío real

## 1. Problema

Una persona puede completar el preformulario y no llegar a ingresar datos en Hotmart. En ese caso Hotmart puede no conocer su identidad y no emitir `PURCHASE_OUT_OF_SHOPPING_CART`.

La ausencia de un webhook no prueba abandono ni ausencia de compra. Sí puede habilitar, después de una espera, un first-touch basado exclusivamente en el hecho autoritativo que ya existe: la persona completó el formulario V1.1.0 y autorizó el contacto comercial por WhatsApp.

## 2. Decisión funcional

```text
lead.precheckout V1.1.0 admitido y autorizado
→ timer durable con due_at = submitted_at + 60 minutos
→ reevaluación transaccional al vencer
→ a lo sumo un first-touch físico
```

Los 60 minutos son un grace period para permitir que una señal Hotmart más específica llegue primero. No son una suposición de SLA ni una ventana máxima de Hotmart.

La garantía contra duplicados es el presupuesto durable compartido por teléfono en `johanna_abandonment_one_shot_commands`. Precheckout diferido, abandono y pago fallido compiten por el mismo first-touch; el primer command reservado consume el presupuesto.

## 3. Prioridad al vencimiento

La reevaluación debe aplicar este orden fail-closed:

1. compra aprobada, opt-out, bloqueo/restricción, takeover o lifecycle terminal → cancelar;
2. identidad, scope, producto, oferta, account/inbox o consentimiento inciertos → bloquear;
3. command físico previo para el teléfono → `budget_consumed`;
4. abandono o pago fallido Hotmart ya admitido para la intención → `superseded_by_provider_event` y cero mensaje genérico;
5. ninguna señal anterior y todas las guardas vigentes → reservar el first-touch precheckout.

Una señal Hotmart posterior se admite y audita normalmente, pero el presupuesto ya consumido impide un segundo command.

## 4. Template propuesto

```yaml
internal_name: johanna_interes_precheckout_01
language: es_EC
category: MARKETING
cadence_position: opening
copy_version: johanna-precheckout-delayed-first-touch-v1
variables:
  - placeholder: "{{1}}"
    semantic_name: buyer_name
    source: latest authorized lead.precheckout V1.1.0 submission
    validation: non-empty bounded text
buttons: []
```

### Body exacto

```text
Hola, {{1}}. Te escribe el equipo de la Psic. Johanna. Vimos que completaste el formulario de Libre de Ansiedad. ¿Quieres que te ayudemos a continuar? Si no deseas recibir más mensajes, responde “No más mensajes”.
```

### Por qué este copy es veraz

- afirma únicamente que el formulario fue completado;
- no afirma que la persona abrió Hotmart, abandonó un carrito, intentó pagar o no compró;
- identifica al remitente como equipo de la Psic. Johanna;
- ofrece ayuda antes de enviar un enlace;
- usa tuteo ecuatoriano neutral y no usa voseo;
- declara un opt-out textual que debe persistirse de forma durable antes de activar el template.

La aprobación interna de este copy no equivale a aprobación Meta. Producción exige que el template exacto figure `APPROVED` en la cuenta WABA y que Chatwoot lo sincronice con el mismo nombre, idioma, categoría y placeholder.

## 5. Autorización y stops

El first-touch sólo puede reservarse si la evidencia durable conserva conjuntamente:

```text
contract_version = 1.1.0
marketing_optin = true
whatsapp_contact = true
copy_version = johanna-precheckout-whatsapp-disclosure-v1
provider_observed = true
activation_authorized = true
whatsapp_contact_authorized = true
lifecycle_state = waiting_for_purchase
```

También deben mantenerse el scope Johanna/Lancemos, producto `F106691755G`, oferta `bxjge6zq`, account/inbox WABA y ownership único del teléfono.

Compra, opt-out, `do_not_contact`, bloqueo, restricción, conflicto de identidad, owner ambiguo, takeover, command anterior y evento Hotmart específico prevalecen sobre la autorización positiva.

## 6. Idempotencia y efecto

- Un replay exacto del formulario reutiliza intención y timer.
- Una submission autorizada posterior para la misma intención conserva el timer
  vivo pero reemplaza su fuente y reinicia `due_at` a 60 minutos desde esa nueva
  submission; una submission anterior no puede adelantarlo.
- Sólo una reevaluación viva puede existir por intención.
- La reevaluación reserva una command `reserved` bajo el mismo ledger físico; el
  RPC de autoridad inmediatamente pre-POST comparte los locks canónicos de
  compra, opt-out, ownership, conversación y presupuesto, y recién allí cambia a
  `request_started`.
- `max_messages=1` y `followups_allowed=0`.
- `delivery_unknown` nunca autoriza reenvío ciego.
- El mensaje no incluye enlace; cualquier enlace posterior se ofrece y envía dentro de la conversación, sujeto a las guardas vigentes.

## 7. Estado de implementación local

La migración local `20260829000300_precheckout_delayed_one_shot_reservation.sql` conecta el vencimiento precheckout con `johanna_abandonment_one_shot_commands`:

- conserva el índice único compartido por `target_phone`;
- agrega una referencia durable e inmutable al timer de origen;
- usa el mismo lock global de Hotmart V2 y el lock por teléfono compartido por abandono y pago fallido;
- revalida compra, señales de proveedor, autorización, opt-out, ownership y takeover antes de reservar;
- completa el timer y crea la command `reserved` en la misma transacción, sin
  declarar todavía request-start;
- deja el helper interno sin `EXECUTE` para roles API;
- exige un scope precheckout dedicado `published`, pero no lo crea ni lo publica;
- no ejecuta efectos externos mientras la capacidad dedicada permanece apagada.

La migración local `20260829000400_precheckout_delayed_worker_sender.sql` y el runtime conectan esa reserva al worker y sender existentes:

- conservan el RPC histórico y agregan un due-list V2 que incluye `precheckout_intent` sólo cuando el proceso envía explícitamente `include_precheckout=true`;
- exponen al `service_role` únicamente el RPC de autorización exacta de la command
  reservada y la última submission V1.1 autorizada de la misma intención/target;
- ese RPC toma los locks compartidos con los stop writers, revalida lifecycle,
  proveedor, autorización, opt-out, ownership, takeover y scope, y sólo entonces
  transiciona atómicamente `reserved → request_started`; si aparece un stop,
  terminaliza `delivery_unknown` y oculta PII;
- usan `PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=false` como gate adicional del proceso;
- construyen por command un sender WABA efímero fenced al `target_phone` durable,
  con `johanna_interes_precheckout_01`, `es_EC`, `MARKETING` y el único
  placeholder `buyer_name`; `ALLOWED_WHATSAPP_JID` no gobierna esta ruta;
- el due-list vuelve a exponer commands `reserved` o `request_started` aunque el
  timer ya esté completo: una proyección fallida reintenta sólo la autoridad, y
  un `request_started` recuperado terminaliza `delivery_unknown` sin POST;
- finalizan la misma command como `accepted_by_chatwoot` o `delivery_unknown` y
  nunca reenvían una respuesta ambigua ni un request-start recuperado;
- conservan IDs remotos al marcar `delivery_unknown` si la finalización de una aceptación falla.
- el POST irreversible corre en un proceso hijo `spawn` terminable: un sender que suprima cancelación no puede retener el shutdown ni continuar el efecto después del corte;
- un shutdown que cancela el sender o su finalización intenta terminalizar `delivery_unknown` dentro de un plazo acotado, tolera cancelaciones repetidas y nunca reenvía la command.

Los tracers PGlite prueban reserva exacta, replay, actualización por submission más
reciente, presupuesto compartido, stops, recuperación de `reserved` y
`request_started`, due-list default-off, autorización pre-send, finalización y ACL.
Probes PostgreSQL 17 reales descartables confirmaron las migraciones y una carrera
de dos sesiones donde un opt-out iniciado primero bloquea la autorización hasta su
commit y produce cero POST. Los tests Python prueban fábrica dinámica real, payload
PostgREST, reintento sólo de una proyección fallida, recuperación sin resend,
respuesta ambigua y cancelación durante sender/finalización. Esto es evidencia
local; no implica merge, despliegue, template aprobado ni activación.

## 8. Promoción y evidencia futura

La implementación local ya cuenta con tests focales/completos, tracers PGlite,
PostgreSQL 17 real descartable y HTTP TCP con lifespan. La promoción exige todavía
evidencia separada de:

1. merge y despliegue default-off de la migración exacta;
2. template Meta exacto en estado `APPROVED` y sincronizado;
3. E2E real: formulario autorizado → 60 minutos → un WhatsApp recibido;
4. señal Hotmart tardía o prueba contractual equivalente → delta de mensajes cero.

Este documento es diseño aprobado con tareas 1–5 terminadas localmente; no es
evidencia de merge, despliegue ni activación. La evidencia local está en
[`2026-08-29-precheckout-delayed-first-touch-local.md`](../operations/2026-08-29-precheckout-delayed-first-touch-local.md).