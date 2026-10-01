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

Sitio, landing, URL y oferta son los de una misma landing del binding: la de la
oferta por defecto (`lead_*`) o una de `additional_offer_landings` (sección
"Una landing por oferta en el precheckout").

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

Con `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED` (apagado por defecto, solo con
manifiesto v2) el bridge llama a `admit_and_plan_portable_lead_precheckout`, que
invoca esa misma admisión sin cambiarla y, si el envío es nuevo, planifica el
primer contacto del formulario. Lo que escribe el plan y cuándo no planifica
está en [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md).
Sin el flag, la admisión es la de arriba y no crea nada más.

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
la secuencia y la primera acción. Una vez que existe
`payment_failure_first_contact`, cualquier pago fallido posterior del mismo caso
se agrega como evidencia y reutiliza esa acción incluso si ya quedó terminal; no
crea otra secuencia ni un segundo contacto inicial. Antes de planificar, la RPC
exige procedencia durable del webhook y correlación resuelta; además comprueba
que tenant, binding activo, producto, oferta, cuenta, inbox, teléfono y contacto
coincidan con el evento y la intención de compra, y deriva el instante de fallo
del payload durable en vez de confiar en el timestamp del caller. El punto de
contacto del teléfono puede venir de Hotmart o del sistema (el bootstrap de la
identidad del formulario crea puntos con fuente `system`).

El evento de pago fallido no concede permiso de contacto por sí solo. Desde la
migración `20260930000100` lo concede, al planificar, la intención de compra con
la que el evento quedó correlacionada, si tiene consentimiento vigente. El
criterio es el de Johanna sin sus valores fijos y vive en
`_portable_consented_intent_reason`, que devuelve `consented_intent_ok` solo si:

- la intención sigue viva: `waiting_for_purchase`, observada por el proveedor,
  no provisional y sin `identity_conflict`, `tracking_incomplete` ni
  `expired_unknown`;
- tiene `whatsapp_contact_authorized` y `activation_authorized`;
- su teléfono es el de destino y es un punto de contacto del contacto. Desde
  `20261001000100` las dos comparaciones son en forma canónica (`52…` ≡ `521…`,
  `54…` ≡ `549…`);
- el teléfono del contacto (`contacts.phone`, que es adonde sale el envío) es
  canónicamente el de la intención (desde `20261001000100`);
- no hay un opt-out de Chatwoot de la cuenta del binding en ninguna de las dos
  formas del teléfono (desde `20261001000100`);
- tiene vinculado un envío `lead.precheckout` 1.1.0 con
  `consent.whatsapp_contact` y `consent.marketing_optin` en `true`, la
  `consent_copy_version` del binding activo y ningún conflicto abierto.

Dos condiciones del criterio de Johanna no se repiten en el helper, a propósito:

- **La ventana entre el formulario y el evento** (Johanna: el pago fallido entre `submitted_at` y `submitted_at + 24 h`). La garantiza la correlación: `correlate_hotmart_purchase_intent` solo resuelve una intención con `submitted_at` en `[observed_at - max_lookback, observed_at]`, con el `max_lookback` de `hotmart_purchase_intent_scopes` de la oferta, y la RPC exige `correlation_outcome = 'resolved'`. Un pago fallido anterior al formulario o posterior al lookback no se correlaciona, se rechaza con `payment_failure_correlation_unresolved` y no concede nada, aunque la intención tenga consentimiento.
- **El nombre y el producto del envío no vacíos** (Johanna los usa como variables de su plantilla). En el camino portable las variables salen del contexto de ejecución del caso y el dispatcher las exige antes de enviar (`template_parameters_missing`); no son parte del permiso.

Si falla algo, el helper devuelve un motivo distinto por cada caso
(`consented_intent_not_live`, `consented_intent_not_authorized`,
`consented_intent_phone_mismatch`, `consented_intent_contact_phone_mismatch`,
`consented_intent_prior_opt_out`, `consented_intent_submission_missing`, entre
otros). No es un entrypoint: ningún rol de la API ni `service_role` lo ejecuta.

Con el contacto bloqueado, y solo si no existe una fila de permiso activa, la RPC
inserta `contact_authorizations` `allowed` con fuente `system` y evidencia
`reason=precheckout_whatsapp_consent`, la intención, el envío, la
`consent_copy_version`, el evento y el caso, y desde `20261001000100`
`phone_match`: `exact` si el teléfono de destino es textualmente el de la
intención, `whatsapp_equivalent` si es la otra forma del mismo móvil. Una fila activa de cualquier estado
gana: un opt-out previo no se pisa y un replay no duplica el permiso. Sin
consentimiento no se concede nada y la reevaluación escala con
`contact_authorization_unknown`.

**Deuda: el permiso es del contacto, no de la aliada.** `contact_authorizations`
no tiene tenant ni funnel (`20260803000100`), `purpose` solo admite
`cart_recovery`, y `reevaluate_followup_action` lo lee por `contact_id` para
cualquier acción durable. Los dos permisos que el producto concede solo tienen
esa forma: el del carrito (`plan_cart_recovery_with_identity`, `20260805000200`,
fuente `hotmart`, vigente desde agosto) y el del pago fallido (`20260930000100`,
fuente `system`). Ninguno lleva `valid_until`, y la aliada queda solo en
`evidence`. Hoy no se cruzan porque cada instancia tiene su base y nunca se
comparte con otra aliada (`docs/instalar.md` §6; la unidad de instancia en
`docs/design/setter-producto-instalable-v1.md` §9): ATT1 corre en su propio
Postgres. Si dos aliadas llegaran a compartir una base y se prendiera el motor
durable para la segunda, un permiso concedido por una habilitaría a la otra
para el mismo contacto. Antes de eso hay que acotarlo: atar `valid_until` a la
vida de la intención o del caso, o que la reevaluación exija que la evidencia
del permiso (`consent_copy_version`, el binding) coincida con la del caso.

El dispatcher selecciona `WABA_PAYMENT_FAILURE_TEMPLATE_NAME` (default
`att1_compra_fallida_01`) y `mark_portable_payment_failure_request_started`
revalida binding, consentimiento, opt-out, límites, lease y canal en la frontera
durable previa al proveedor.

`FinalMetaEffectGate` se evalúa después de componer y validar el efecto, pero
antes de `request_started` y antes del sender. Con el gate cerrado se registra
evidencia sanitizada `final_meta_gate_closed`/`final_effect_blocked`; no se inicia
el request y no se representa el efecto como aceptado, enviado o entregado.
Habilitar la admisión no habilita el request HTTP a Meta.
Además del default-off general, un manifiesto cuyo `tenant_ref` sea `att1`
rechaza el startup cuando `META_FINAL_EFFECT_ENABLED=true`. Abrir ese último gate
requiere un cambio de release explícito; no puede hacerse sólo mediante una
variable de entorno en la versión actual.

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
oferta, dentro del lookback provisionado. Desde `20261001000100` el teléfono se
compara por sus dos formas en las consultas de candidatos. Los outcomes append-only son
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
La resolución de política tampoco agenda por sí sola el mensaje posterior a una
respuesta inbound. Ese planificador y el transporte exacto de la variable de
cupón permanecen bloqueados hasta que exista una plantilla WABA aprobada con su
contrato de componentes; el runtime no reutiliza `no_reply_review` ni inventa una
plantilla para cubrir ese hueco.

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

## Varias ofertas por binding (2026-09-28, migración `20260928000200`)

Una aliada vende el mismo producto con una oferta por landing. El binding suma `additional_offer_codes`: una lista de hasta 16 códigos alfanuméricos distintos, sin repetir `offer_code`. `offer_code` sigue siendo la oferta por defecto: la del checkout cuando no se sabe qué landing vio la persona. En el manifiesto JSON la clave es opcional y por defecto está vacía. Con eso, un binding de una sola oferta se comporta como antes.

- **Carrito abandonado y pago fallido:** se aceptan con cualquier oferta del binding, en el bridge (`accepted_offer_codes`) y en las RPC. Cada evento resuelve el scope de intención de **su** oferta (`hotmart_purchase_intent_scopes`), que tiene que existir y estar activo. Una oferta fuera del binding se rechaza sin crear eventos.
- **Compra aprobada:** se acepta con **cualquier oferta del producto** y frena las intenciones de ese producto, sin importar la oferta. Una compra es una compra aunque entre por una oferta que el setter no ofrece.
- **Pago fallido que este runtime no procesa** (otro producto, una oferta fuera del binding): responde `200 {"status":"ignored","reason":"invalid_payment_failure_payload"}`, igual que el carrito y la compra. Antes era `422`, y Hotmart cuenta los 4xx como fallas del webhook hasta desactivarlo.
- **Lo que esta migración dejó sin multi-oferta:**
  - el precheckout portable (una sola landing por binding), resuelto en `20260930000200` (sección siguiente);
  - la frontera del piloto que autoriza el envío (`pilot_scope_versions.offer_code`, comparada en `authorize_lancemos_pilot_request_start`), resuelta en `20260929000100` con `pilot_scope_versions.additional_offer_codes`.

  Hasta esas dos migraciones, un evento de una oferta adicional quedaba admitido y correlacionado, pero su envío no estaba autorizado.

## Una landing por oferta en el precheckout (2026-09-30, migración `20260930000200`)

Las landings de una aliada pueden estar en sitios y hosts distintos: ATT1 tiene dos en `www.metodoraizana.com` y una en `site.metodoraizana.com.mx`. El binding suma `additional_offer_landings`, una lista con la landing de cada oferta adicional, en el mismo orden que `additional_offer_codes`:

```json
"additional_offer_landings": [
  {
    "offer_code": "second-offer",
    "site": "ally-one-site",
    "landing_id": "second",
    "page_host": "ally-one.example",
    "page_path": "/offer/second"
  }
]
```

- **Forma:** cada objeto tiene exactamente esas cinco claves, con las mismas reglas que `lead_*` (slugs, hostname canónico, path absoluto sin query ni fragment). La lista va vacía o con una landing por oferta adicional, en su orden, y ninguna repite el sitio y la landing de otra ni de la oferta por defecto. En la base lo exige el check `commercial_ally_runtime_bindings_offer_landings_shape`; en el bridge, `CommercialAllyConfig`.
- **Vacía** (el valor por defecto, y el de toda fila anterior a la migración): el formulario entra solo por la landing de la oferta por defecto, como antes. En el manifiesto JSON la clave es opcional.
- **Admisión:** `admit_portable_observed_lead_precheckout` resuelve la oferta del envío (`commerce.offer_ref`) entre la por defecto y las que tienen landing declarada, y exige la landing, el sitio, el host y la ruta de **esa** oferta. Una oferta sin landing declarada se rechaza con `observed_precheckout_assurance_mismatch`, como una oferta ajena. El parser del bridge y `/webhooks/lead` aplican la misma terna (sitio, landing, oferta).
- **Una intención por oferta:** la misma persona en dos landings deja dos intenciones, una por oferta, como el modelo de seis landings de Johanna. Así el carrito y el pago fallido de cada oferta encuentran su intención.
- **Manifiesto v2:** `to_commercial_ally_config` llena la lista con el sitio, la landing y la URL de cada oferta de `[[hotmart.ofertas]]` que no es la por defecto.
- **Orden de despliegue:** la migración, después la fila del binding con sus landings, y después el bridge. Un bridge nuevo con manifiesto sobre la fila vieja ve drift: `/ready` responde `503 commercial_ally_binding_unavailable` y, con la proyección de Slack prendida, el arranque falla. Una fila ya sembrada se completa en el lugar (no con un `binding_version` nuevo, que dejaría sin planificar los eventos admitidos con la versión vigente); el `UPDATE` exacto para ATT1, con su verificación, está en `docs/instalar.md` (paso 9, "Actualizar el bridge de una instancia que ya tiene la base sembrada"). Antes de desplegar hay que confirmar si la fila de la instancia ya existe.

## Equivalencia de teléfonos de WhatsApp (2026-10-01, migración `20261001000100`)

El mismo móvil llega con dos formas. El formulario (el adaptador de GHL y `/webhooks/lead`) guarda `52` + 10 dígitos en México y `54` + 10 en Argentina; Hotmart y el `wa_id` de WhatsApp traen `521` + 10 y `549` + 10. Con la comparación exacta, el lead que dejó el formulario se perdía antes del permiso: el correlador lo encontraba por email y no por teléfono, daba `conflict`, la intención quedaba en `identity_conflict` y el pago fallido moría en `payment_failure_correlation_unresolved`. La compra portable tampoco marcaba `purchased`.

**La regla.** Se compara en forma canónica y **no se reescribe nada al guardar**: lo guardado se sigue validando contra el payload crudo de cada fuente.

- `_whatsapp_phone_canonical(text)`: deja solo los dígitos y reescribe únicamente `521` + 10 dígitos a `52` + 10 y `549` + 10 a `54` + 10. Va anclada por largo (13 dígitos): un nacional de 10 dígitos que empieza con 1 o con 9 queda igual a sí mismo.
- `_whatsapp_phone_variants(text)`: las formas que comparten canónica (una o dos), para buscar con `= any(...)`.
- Brasil (el noveno dígito) queda afuera: no hay medición.
- Las dos son privadas: ningún rol de la API las ejecuta. `bridge/phones.py` es su espejo en Python.

**Qué cambia en la base.** La migración solo crea y reemplaza funciones; no toca tablas ni filas.

- `_correlate_portable_hotmart_purchase_intent(uuid)`: el correlador portable, derivado del compartido ([hotmart-purchase-intent-correlation-v1.md](hotmart-purchase-intent-correlation-v1.md), §8). `admit_portable_hotmart_cart_abandonment` y `admit_portable_hotmart_payment_failure` pasan a llamarlo.
- `admit_portable_hotmart_purchase_approved` compara el teléfono por sus formas.
- `_portable_consented_intent_reason` compara el destino y el punto de contacto en forma canónica, y suma dos chequeos:
  - `consented_intent_contact_phone_mismatch`: `contacts.phone`, que es adonde sale el envío, tiene que ser canónicamente el teléfono consentido;
  - `consented_intent_prior_opt_out`: no hay un opt-out de Chatwoot (`contact_opt_out_events`) de la cuenta del binding en ninguna de las dos formas, en los estados que frena el arranque del envío. Un opt-out guardado como `unmatched` bajo `521…` no frenaba a una identidad `52…`.

  Los dos corren al planificar y al arrancar el envío, en carrito y en pago fallido, en los modos de audiencia con consentimiento (el helper lo usa la frontera del piloto a través de `_lancemos_pilot_audience_intent`, que no se redefine). La comparación del envío del formulario con su propia intención queda exacta: son la misma fuente.
- `plan_portable_payment_failure_recovery` compara en forma canónica y deja `phone_match` en la evidencia del permiso.
- Los dos arranques del piloto que no son el del primer contacto, `mark_lancemos_pilot_request_started` (carrito) y `mark_portable_payment_failure_request_started`, miran el opt-out en todas las formas antes de arrancar, en los tres modos de audiencia. El helper privado `_portable_chatwoot_opt_out_stop` toma en orden el lock de opt-out de cada forma de la identidad del caso y de `contacts.phone` (adonde sale el envío) y busca un opt-out de Chatwoot de la cuenta en cualquiera, en los estados que frena el arranque compartido. Si lo hay, rechaza con `pilot_request_start_rejected` / `pilot_chatwoot_opt_out_stop` sin consumir cupo. Hace falta por `manual_cohort`: ahí no corre el chequeo del consentimiento, y el arranque compartido busca el id exacto de la identidad, así que un opt-out `unmatched` bajo `521…` no frenaba el carrito de una identidad `52…` ni el pago fallido que usa el permiso de ese carrito, y el envío salía a ese mismo `wa_id`. Con ese opt-out la reevaluación sigue ejecutando: el intento queda reservado y el arranque lo rechaza en cada lease hasta que la acción vence. Detalle: [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), §3 y §9.
- `reserve_portable_checkout_issuance_v2`: la reserva del enlace de pago del entrante para el runtime portable, solo para `service_role`. Se deriva de la definición vigente de `reserve_chatwoot_checkout_issuance_v2` (`20260927000200`) con `pg_get_functiondef` + `replace`, como el correlador, con la cantidad exacta de cada texto (si no, la migración falla con `55000`). Cambian el nombre, las tres búsquedas de la intención del móvil (la compra previa, la intención del lead y la intención de la emisión), que pasan a `= any(_whatsapp_phone_variants(...))`, y el chequeo del opt-out, que mira cada forma. Quedan exactos el chequeo de la identidad del caso, el del replay y la intención que se inserta cuando no hay ninguna viva en ninguna forma. La compartida usa el mismo `p_external_user_id` para exigir la identidad y para buscar la intención: con la identidad en `521…` y la intención del formulario en `52…`, el enlace salía con la oferta por defecto (`default_no_intent`), sin el `sck` ni el `fbclid` del formulario, la reserva fabricaba una segunda intención viva bajo `521…` y no veía la compra de quien ya había comprado, así que le mandaba otro enlace.
- **Un cambio de resultado:** con un opt-out previo, un pago fallido en un scope con consentimiento ya no se planifica (`pilot_scope_rejected` con `pilot_audience_consented_intent_prior_opt_out`). Antes se planificaba y lo frenaba la reevaluación.

**Qué cambia en el bridge, solo con manifiesto.**

- **Resolución del evento de Hotmart.** `resolve_event` busca el contacto por las formas equivalentes del teléfono (`find_contact_by_phones`); es ambiguo solo si las filas son de contactos distintos. El punto de contacto se sigue guardando crudo. La identidad de WhatsApp también, **salvo que el contacto ya tenga una para ese móvil en la otra forma**: si el contacto ya existía y tiene exactamente una identidad activa del inbox entre las formas del teléfono, y no es la cruda, el plan reutiliza esa (`resolution_whatsapp_identity_reused`). Sin eso, quien recibió el primer contacto del formulario (identidad `52…`) y después llegaba al checkout (Hotmart `521…`) quedaba con dos identidades activas, y con dos el entrante usa el `wa_id` textual y choca con la conversación de la otra. Los planificadores comparan ese id con el teléfono consentido en forma canónica, así que cualquiera de las dos formas es el mismo destinatario. Si la lectura falla, se planifica con el teléfono crudo, como antes, y queda `resolution_whatsapp_identity_lookup_failed` en el log (con el id del contacto y la región, sin el número). Con las dos identidades ya creadas (un duplicado anterior) no se elige por el contacto: sigue la cruda.
- **Mensaje entrante.** Si entre las formas del `wa_id` hay exactamente una identidad activa del inbox y no es la textual, se usa la guardada: así el opt-out, la admisión y el enlace caen en el contacto que ya existe. Con más de una se usa la textual, con un warning que lleva ids de Chatwoot y la región, nunca el número. Si la lectura falla, el trabajo queda para reintentar. Lo que se valida contra Chatwoot sigue siendo el `wa_id` textual.
- **Enlace de pago del entrante.** El bridge reserva el enlace con `reserve_portable_checkout_issuance_v2` (mismo payload, misma respuesta) y el `external_user_id` resuelto. Resolver el contacto no alcanza para encontrar la intención: sin identidad guardada (quien dejó el formulario y escribe por su cuenta) o con la identidad en la forma del `wa_id` (quien escribió antes del formulario) el id que llega es `521…` y la intención del formulario está en `52…`. Con la reserva portable, ese enlace lleva la oferta de la landing del formulario, su `sck` y su `fbclid`, y apunta a su intención; quien ya compró recibe `purchase_already_approved`, que el bridge pasa a una derivación como cualquier enlace bloqueado. Autorizar y cerrar la emisión no cambian: trabajan por `issuance_id`. Lo prueba `validate_whatsapp_phone_equivalence.mjs` (10), con el scope entrante y el catálogo sembrados como `aprovisionar-att1.sql` de la instancia (el hotlink como `external_product_id`).
- **Opt-out del entrante.** El chequeo que frena una respuesta mira el opt-out en todas las formas, no solo en la resuelta.
- **Proyecciones a Chatwoot** (la macro de opt-out, la asignación y la nota de la derivación): prueban la forma guardada y, ante `conversation_identity_mismatch`, la otra forma del mismo móvil.
- **Primer contacto por el dispatcher.** El destinatario se resuelve antes del gate final, buscando las dos formas en Chatwoot ([approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md), "El destinatario").

**Con qué forma se manda** y se crea el contacto de Chatwoot lo decide `whatsapp_delivery_phone`: México como `521` + 10, Argentina como `54` + 10, cualquier otro igual a sí mismo. Sale de una medición del 2026-10-01 sobre el Chatwoot de producción (versión 4.13), detallada en [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md#teléfonos-las-dos-formas-del-mismo-móvil). La medición es de otro inbox y no prueba el envío en ATT1: ahí se confirma en el E2E.

**Fuera de este cambio.** Los seguimientos (`no_reply_review`) siguen usando el teléfono del contacto tal como está guardado. La reactivación y el descuento post-respuesta comparan el `external_user_id` exacto, y el enlace del descuento (`claim_conversation_followup_v1`) sale por la reserva compartida: ATT1 no siembra políticas de descuento.

**Johanna no cambia.** No se redefine nada de lo que ejecuta: `correlate_hotmart_purchase_intent`, `_admit_hotmart_purchase_intent_identity`, sus admisiones, el opt-out de Chatwoot, `mark_followup_request_started`, `reevaluate_followup_action` ni `reserve_chatwoot_checkout_issuance_v2`. Los dos arranques del piloto los llama el bridge solo con `LANCEMOS_PILOT_BOUNDARY_ENABLED`, que en Johanna está apagado: sin la frontera arranca por `mark_followup_request_started`. Lo de Python entra solo con manifiesto: sin él, el enlace sigue saliendo por la reserva compartida con el `wa_id` textual. `validate_whatsapp_phone_equivalence.mjs` compara `pg_get_functiondef` de todas las funciones de `public` antes y después de la migración: cambian las siete reemplazadas, se suman cinco (cuatro helpers privados y la reserva portable) y ninguna otra.

Por eso el defecto del enlace sigue en Johanna: un lead mexicano que dejó el formulario con `52…` y escribe desde `521…` recibe el enlace sin la intención del formulario (oferta por defecto, sin su `sck`). Es lo medido en sus conversaciones 172, 184 y 211. Arreglarlo cambia el comportamiento de la instancia que factura y queda para una decisión aparte.

## Entrante primero, Hotmart después (límite conocido, 2026-10-01)

Quien escribió primero por WhatsApp tiene un contacto sin email, teléfono ni puntos: solo la identidad con su `wa_id`. Si después abandona el carrito o falla el pago, `resolve_event` busca por email y por punto de teléfono y no lo encuentra; al planificar, la identidad del mismo `wa_id` ya es de otro contacto (`channel_identity_contact_mismatch`) y el evento queda `create_recovery_case_failed`. Falla cerrado: se pierde esa recuperación y no se manda nada indebido. Sale de leer el código; ningún test fija todavía ese recorrido. El primer contacto del formulario sí cubre el caso (busca el contacto también por identidad y le completa los datos).
