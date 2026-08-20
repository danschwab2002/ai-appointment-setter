# Contrato de ingreso de abandono de carrito Hotmart v1

- **Estado:** Expand, bridge, E2E controlado y contract `20260820000400` verificados en
  Cloud. Postflight: shim legacy denegado y wrapper canónico disponible.
  Sigue pendiente evidencia de una entrega originada oficialmente por Hotmart.
- **Evento:** `PURCHASE_OUT_OF_SHOPPING_CART`
- **Versión de payload:** `2.0.0`
- **Endpoint:** `POST /webhooks/hotmart`

## Autenticación del transporte

El receptor exige `X-Hotmart-Hottok` y compara el valor con `HOTMART_HOTTOK` mediante comparación constante. Un Hottok ausente o incorrecto produce `401 {"detail":"invalid_token"}` y no persiste datos.

## Payload procesable

El ingreso autoritativo requiere:

- `id`: string no vacío;
- `event = PURCHASE_OUT_OF_SHOPPING_CART`;
- `version = 2.0.0`;
- `creation_date`: entero positivo en milisegundos Unix;
- `data.buyer.email` o teléfono normalizable desde `phone`; si `phone` está
  ausente o es inválido, se usa `checkout_phone` como fallback;
- `data.product.id`: entero positivo;
- `data.product.name`: string no vacío;
- `data.offer.code`: string no vacío.

La clasificación HTTP ocurre por etapas y ninguna respuesta inválida reserva la
identidad durable:

- ID ausente → `200 ignored / missing_event_id`;
- tipo no soportado → `200 ignored / unsupported_event_type`;
- versión no soportada → `200 ignored / unsupported_version`;
- fecha ilegible → `400 {"detail":"invalid_creation_date"}`;
- fecha fuera de la ventana de frescura —incluido `0`— →
  `401 {"detail":"stale_webhook"}`;
- abandono con fecha fresca pero sin los demás campos procesables →
  `200 ignored / invalid_cart_abandonment_payload`.

La RPC SQL rechaza directamente cualquier `creation_date <= 0` o payload no
procesable con `invalid_cart_abandonment_admission_input`.

## Admisión semántica y correlación

La frontera canónica es
`public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)`, que
admite y correlaciona atómicamente. Durante el rolling deploy, la firma histórica
`public.admit_hotmart_cart_abandonment(text,jsonb)` permaneció como shim seguro. La
fase contract `20260820000400`, aplicada en Cloud después del despliegue de la imagen
contract, revoca su ejecución para `service_role`.

La tupla semántica canónica está formada por:

- email normalizado;
- teléfono normalizado;
- producto externo;
- nombre de producto normalizado;
- oferta;
- fecha de abandono.

Resultados:

| `outcome` | HTTP | Significado |
|---|---:|---|
| `inserted` | 202 | Se creó un `webhook_events` procesable. |
| `duplicate` | 200 | Repetición exacta; se reutiliza el mismo `webhook_event_id`. |
| `semantic_conflict` | 200 | El mismo `external_event_id` llegó con otra tupla semántica. No se procesa. |

Los payloads inválidos fallan con una constraint SQL antes de insertar o reservar la clave idempotente.

## Conflictos y fail-closed

Un conflicto se registra en `hotmart_cart_abandonment_semantic_conflicts` con los payloads existente y entrante. Mientras exista cualquier conflicto no resuelto, `guard_cart_abandonment_semantic_conflict_request_start` bloquea globalmente el inicio de nuevos requests outbound con `unresolved_cart_abandonment_semantic_conflict`.

Resolver requiere establecer `resolved_at` y `resolution`; no hay endpoint público para hacerlo.

## Resolución de identidad

Se consultan siempre email y teléfono cuando ambos existen.

- cero matches: puede crearse un contacto nuevo según el flujo existente;
- un único contacto común: se reutiliza;
- más de un match para un identificador, o email y teléfono asociados a contactos distintos: el evento pasa a `failed` con `identity_ambiguous` y no se crea caso/plan.

Si la materialización durable de cualquiera de los `contact_points` falla, el
evento pasa a `failed` con `identity_binding_failed`; no se intenta planificar.

## Binding del plan

Antes de insertar un `recovery_case_events(event_role = 'cart_abandonment')`, el trigger `validate_hotmart_cart_recovery_binding` comprueba dentro de la transacción:

- origen, tipo, versión y procesabilidad del evento;
- igualdad de producto, nombre, oferta y timestamp entre payload y caso;
- que todos los identificadores del payload resuelvan a exactamente un contacto y que sea el contacto del caso.

Un mismatch aborta toda la planificación. La RPC existente `plan_cart_recovery_with_identity` conserva la planificación, el binding de canal y el grant `source = hotmart`, sin pisar denegaciones u opt-out.
Las columnas canónicas del caso, el vínculo con el evento y la identidad/payload
del evento fuente quedan inmutables después de crearse. La protección rechaza
cualquier `UPDATE` o `DELETE` del vínculo. Un evento `source = simulator` nunca puede otorgar autoridad
Hotmart.

## Orden de precedencia

Los blockers durables de opt-out/denegación y los conflictos semánticos se reevalúan antes de `request_started_at`; el envío falla cerrado aunque el evento hubiera sido admitido previamente.

## Privilegios

Sólo los wrappers canónicos pueden ejecutarse con `service_role`. Los shims históricos,
las implementaciones base y las funciones internas de identidad, validación y guards no
tienen `EXECUTE` para roles API. La revocación contract se materializa en `20260820000400`.
El postflight Cloud confirma la ACL efectiva y el rechazo HTTP del shim con `service_role`.
