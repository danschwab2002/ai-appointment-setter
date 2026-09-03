# Contrato de runtime por aliada comercial v1

- Estado: implementado localmente para configuración, readiness, lead durable y stop de compra sin efectos
- Fecha: 2026-09-01
- Diseño: `docs/design/portable-single-tenant-runtime-v1.md`

## Manifiesto no secreto

`COMMERCIAL_ALLY_CONFIG_PATH` apunta a un JSON montado dentro del runtime. Debe ser un objeto con exactamente estas claves:

```json
{
  "tenant_ref": "att1",
  "funnel_ref": "att1-main",
  "binding_version": 1,
  "ally_ref": "ally-one",
  "lead_ally_name": "Ally One",
  "lead_site": "ally-one-site",
  "lead_landing_id": "main",
  "lead_page_host": "ally-one.example",
  "lead_page_path": "/offer/main",
  "product_hotlink": "PRODUCT_HOTLINK",
  "product_name": "Approved product name",
  "product_price": "49",
  "currency": "USD",
  "offer_code": "approved-offer",
  "consent_copy_version": "approved-whatsapp-consent-v1",
  "hotmart_product_id": 123456,
  "chatwoot_account_id": 42,
  "chatwoot_inbox_id": 24,
  "inbound_scope_key": "att1-inbound",
  "inbound_scope_version": 1
}
```

Los valores son ilustrativos y no autorizan ATT1. Deben sustituirse con información confirmada por la responsable operativa y la aliada.

## Validación local

- referencias y scopes: slugs canónicos;
- `binding_version`, IDs Chatwoot, versión inbound y producto Hotmart: enteros positivos, no booleanos;
- host: hostname canónico sin esquema, credenciales, puerto, query ni fragment;
- path: absoluto y sin query/fragment;
- precio: decimal finito y positivo;
- moneda: tres letras mayúsculas;
- ninguna clave adicional o ausente es aceptada.
- cada campo booleano de `Settings` exige tipo Python exacto `bool`; valores como `1`, `"true"`, `null`, listas u objetos impiden construir la aplicación.

Los secretos no están permitidos en este manifiesto. Hottok, tokens, API keys y firmas permanecen en el secret store.

## Autoridad durable

Tabla: `public.commercial_ally_runtime_bindings`.

Clave primaria:

```text
(tenant_ref, funnel_ref, binding_version)
```

El runtime resuelve exclusivamente mediante:

```text
public.resolve_commercial_ally_runtime_binding(
  p_tenant_ref text,
  p_funnel_ref text,
  p_binding_version integer
)
```

La función devuelve una fila sólo cuando su estado es `active`. No se insertan bindings automáticamente. La tabla tiene RLS habilitado; `anon` y `authenticated` no tienen acceso. La migración revoca primero los privilegios de tabla heredados por `service_role` y vuelve a conceder sólo `select`; ese rol conserva `execute` únicamente para la lectura.

## Readiness

Para cualquier manifiesto suministrado, incluso si sus valores coinciden con el binding legado:

- fila activa exacta y sin drift: `commercial_ally_binding=active`;
- Supabase ausente, RPC ausente, cero/múltiples filas, estado distinto de `active`, forma inválida o cualquier diferencia: HTTP `503`, detalle `commercial_ally_binding_unavailable`.

`/health` continúa indicando vida del proceso y no prueba que el binding esté activo.

## Scope de ingresos

### Lead precheckout

El payload debe coincidir con el manifiesto en:

- sitio;
- aliada declarada;
- landing y URL;
- hotlink, nombre, precio y moneda;
- checkout y offer code;
- versión del consentimiento.

El parser construye un evento canónico con `tenant_ref` y `funnel_ref` del
manifiesto. Cuando la procedencia es un manifiesto explícito,
`LEAD_PRECHECKOUT_ENABLED=true` usa
`admit_portable_observed_lead_precheckout(tenant_ref, funnel_ref,
binding_version, ...)`; los tres identificadores son server-owned. La RPC
`SECURITY DEFINER` bloquea y relee la fila activa exacta y rechaza ausencia,
inactividad o drift de tenant, funnel, landing, URL, aliada, producto, nombre,
oferta, precio, moneda o `consent_copy_version` antes de escribir.

La transacción conserva las semánticas existentes de `inserted`, `duplicate` y
`semantic_conflict` y sólo escribe `precheckout_submissions`, `purchase_intents`,
`purchase_intent_submissions` y, para conflicto, su tabla append-only. No agenda
reevaluaciones ni crea actions, commands, messages o delivery attempts. La RPC
legada `admit_observed_lead_precheckout` no cambió.

### Hotmart salida de carrito portable

`PORTABLE_HOTMART_RECOVERY_ENABLED` es `false` por defecto y requiere un
manifiesto explícito. Para `PURCHASE_OUT_OF_SHOPPING_CART` versión `2.0.0`, la
RPC `admit_portable_hotmart_cart_abandonment` bloquea el binding activo exacto,
deriva producto, oferta y scope server-side, y sólo admite/correlaciona el evento.
No crea timers, actions, commands, mensajes ni efectos outbound.

Cada evento portable nuevo queda ligado en
`commercial_ally_hotmart_event_bindings` al tenant, funnel, versión de binding,
UUID exacto de `hotmart_purchase_intent_scopes`, producto Hotmart, producto de
intención y oferta usados al admitirlo. La FK al scope usa `ON DELETE RESTRICT` y
un trigger bloquea físicamente `UPDATE` y `DELETE`, incluso para el owner. Un
replay `duplicate` o `semantic_conflict` debe encontrar esa procedencia y
coincidir con ella exactamente; ausencia o drift falla cerrado antes de
reutilizar la correlación. La tabla no es accesible directamente por roles API
ni por `service_role`; sólo la RPC `SECURITY DEFINER` la administra.

Cuando el manifiesto explícito y todos los fences WABA coinciden, el factory
construye un `ChatwootMessageSender` con capability dinámica explícita en vez de
un JID fijo. El `DurableDispatcher` rechaza ese sender si falta
`FinalMetaEffectGate`; el gate permanece default-off y registra evidencia
sanitizada antes de cualquier `request_started` o llamada al proveedor.

### Hotmart pago fallido

`PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED` es `false` por defecto y requiere un
manifiesto explícito. Para `PURCHASE_CANCELED` versión `2.0.0`, producto y oferta
deben coincidir exactamente con `hotmart_product_id` y `offer_code`; una
configuración ATT1 rechaza eventos Johanna y viceversa durante parsing.

`admit_portable_hotmart_payment_failure` fija tenant, funnel y versión desde el
manifiesto, persiste el evento con idempotencia durable, conserva conflicto
semántico y lo correlaciona como `payment_failure_supported`. El trigger mantiene
identidad propia en todo el recorrido: `event_role=payment_failure`,
`trigger_kind=payment_failure` y `anchor_type=payment_failure`; no reutiliza la
semántica de abandono confirmado.

`plan_portable_payment_failure_recovery` crea o reutiliza atómicamente el caso,
la secuencia y la primera acción. El evento de pago fallido no concede permiso
de contacto: la autorización debe existir antes de iniciar la salida y se
comprueba en esa frontera. Antes de planificar, la RPC exige procedencia durable
del webhook y correlación resuelta; además comprueba que tenant, binding activo,
producto, oferta, cuenta, inbox, teléfono y contacto coincidan con el evento y
la intención de compra, y deriva el instante de fallo del payload durable en vez
de confiar en el timestamp del caller. El dispatcher selecciona
`WABA_PAYMENT_FAILURE_TEMPLATE_NAME` (default
`att1_compra_fallida_01`) y `mark_portable_payment_failure_request_started`
revalida binding, consentimiento, opt-out, límites, lease y canal en la frontera
durable previa al proveedor.

`FinalMetaEffectGate` se evalúa después de componer y validar el efecto, pero
antes de `request_started` y antes del sender. Con el gate cerrado se registra
evidencia sanitizada `final_meta_gate_closed`/`final_effect_blocked`; no se inicia
el request y no se representa el efecto como aceptado, enviado o entregado.
Habilitar la admisión no habilita el request HTTP a Meta.

### Hotmart compra aprobada portable

`PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` es `false` por defecto. Sólo un runtime
con manifiesto explícito puede combinarlo con `HOTMART_HOTTOK`; todos los demás
flags heredados continúan rechazados. En este modo el handler autentica el request,
ignora `PURCHASE_CANCELED` y `PURCHASE_OUT_OF_SHOPPING_CART` con el reason code
sin PII `portable_purchase_stop_event_ignored`, y sólo procesa
`PURCHASE_APPROVED` versión `2.0.0` con producto y oferta exactamente iguales al
binding.

La frontera durable es:

```text
public.admit_portable_hotmart_purchase_approved(
  p_tenant_ref text,
  p_funnel_ref text,
  p_binding_version integer,
  p_external_event_id text,
  p_payload jsonb,
  p_normalized_email text,
  p_normalized_phone text
)
```

Tenant, funnel y versión proceden del manifiesto server-side. La RPC bloquea la
fila `active`, vuelve a validar `hotmart_product_id` y `offer_code`, y exige una
fila explícita `enabled=true` en
`commercial_ally_hotmart_purchase_policies`. Esta tabla no tiene seed, su default
es `enabled=false` y `max_lookback` es la única política temporal durable; no se
presupone una ventana de 24 horas.

La correlación considera exclusivamente intents del mismo binding, producto y
oferta, dentro del lookback provisionado. Los outcomes append-only son
`resolved`, `unmatched`, `ambiguous` y `conflict`. Sólo `resolved` cambia el intent
exacto a `purchased`, desactiva `activation_authorized` y cancela o supersede
atómicamente cualquier reevaluación ya existente. Los otros outcomes no mutan
intents ni crean efectos. La admisión conserva `inserted`, `duplicate` y
`semantic_conflict`; replay exacto no duplica correlación y conflicto semántico
no ejecuta stop.

Este corte no crea abandonment scheduling, workers, replay, recovery cases,
scheduled actions, commands, messages, delivery attempts ni outbound. Las RPCs
Hotmart legadas y su comportamiento permanecen sin cambios.

### Política de descuento versionada

`commercial_ally_discount_policy_versions` conserva una política por binding,
trigger, clave y versión. Producto y oferta quedan fijados por la versión exacta
del binding referenciado. Toda versión nace como `draft` y sólo admite las
transiciones `draft → approved → published → retired`; una versión aprobada es
inmutable y sólo puede existir una versión `published` por binding y trigger.

La superficie runtime es exclusivamente de lectura:

```text
public.resolve_commercial_ally_discount_policy(
  p_tenant_ref text,
  p_funnel_ref text,
  p_binding_version integer,
  p_trigger_kind text
)
```

El resolver devuelve una fila únicamente cuando binding y política están
activos/publicados y dentro de vigencia. `service_role` no posee lectura directa
ni DML sobre la tabla; sólo puede ejecutar el resolver. La migración no siembra
políticas, por lo que el resultado inicial es vacío y fail-closed.

La política fija tipo/valor del descuento, referencia de cupón, duración de la
oferta, posición existente (`first_touch` o `later_step`) y versiones exactas de
template/copy. Publicarla no crea timers, acciones, comandos, mensajes o intentos
de entrega, no modifica la cadencia y no autoriza contacto ni outbound.

### Chatwoot inbound

Account, inbox, scope key y scope version del manifiesto se validan localmente. Como la cadena heredada de admisión, agente, stops y respuestas aún no está completamente parametrizada, `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, Cut B o respuestas automatizadas no pueden habilitarse con un manifiesto no legado.

## Compatibilidad y prohibiciones

- Sin manifiesto sólo se admite compatibilidad legada con Chatwoot account `1` e inbox `9` o no configurado.
- Cualquier otro account/inbox sin manifiesto impide iniciar.
- La procedencia del manifiesto se conserva por separado de sus valores. Un archivo suministrado nunca entra en compatibilidad legada, aunque copie exactamente todos los valores Johanna.
- El estado `approved` no equivale a `active`.
- Binding activo no equivale a autorización de contacto, activación comercial, deploy o envío real.
- Para cualquier manifiesto suministrado, startup permite únicamente
  `LEAD_PRECHECKOUT_ENABLED=true` y/o
  `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=true` y/o
  `PORTABLE_HOTMART_RECOVERY_ENABLED=true`. `HOTMART_HOTTOK` sólo se admite con
  `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=true`. Cada otro flag booleano debe
  ser exactamente `False`.
- Las rutas outbound heredadas siguen fuera de este contrato.
