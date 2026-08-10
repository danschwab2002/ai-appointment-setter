# Evidencia local: ingreso autoritativo de abandono de carrito Hotmart

- **Fecha:** 2026-08-10
- **Estado:** implementación y validación local completadas; no desplegada
- **Workstream:** B — `PURCHASE_OUT_OF_SHOPPING_CART`
- **Base auditada:** `origin/main` en `0fd2a26edac4dddc0913ff0014b9455517396c35`

## Alcance verificado

- validación estricta del payload Hotmart v2.0.0 antes de persistir;
- admisión transaccional con replay exacto y conflicto semántico;
- evidencia durable del conflicto y bloqueo fail-closed antes de iniciar outbound;
- rechazo de identidades ambiguas por email/teléfono;
- binding SQL exacto entre evento, contacto, producto, oferta, timestamp y caso;
- preservación de autorización Hotmart y precedencia de opt-out existentes.

## Suite focalizada

```text
uv run pytest tests/test_e2e.py tests/test_hotmart_webhook.py \
  tests/test_resolution.py tests/test_hotmart_cart_abandonment_migration.py -q
.................................................... [100%]
```

Resultado inicial focalizado: **52 tests aprobados**. Luego se agregó una
aserción estructural para el `product.id` entero positivo y la suite completa
volvió a pasar al cierre.

## Validación SQL ejecutable

```text
npm --prefix tests/sql/followup_engine test
```

Resultado: exit `0`. Además de toda la regresión durable previa, el nuevo validador confirmó:

```text
cart_abandonment_migration_apply=OK
cart_abandonment_checkout_phone_canonical=OK
cart_abandonment_exact_replay=OK
cart_abandonment_plan_binding_reject=OK
cart_abandonment_product_name_binding_reject=OK
cart_abandonment_each_identifier_bound=OK
cart_abandonment_simulator_authority_reject=OK
cart_abandonment_plan_authorized=OK
cart_abandonment_binding_update_bypass_reject=OK
cart_abandonment_binding_any_update_reject=OK
cart_abandonment_binding_delete_bypass_reject=OK
cart_abandonment_case_binding_immutable=OK
cart_abandonment_source_event_immutable=OK
cart_abandonment_semantic_conflict=OK
```

## Revisión independiente

Una revisión read-only independiente devolvió `request_changes` por cinco
brechas: `limit=1` ocultaba identidades múltiples, faltaba bindear el nombre de
producto, bastaba uno de dos identificadores, un evento `simulator` podía llegar
al grant Hotmart y el binding podía reescribirse después del insert. También
detectó desalineaciones del contrato HTTP/SQL.

Todas fueron corregidas con pruebas de regresión. Los lookups consultan hasta
dos filas, cada identificador presente debe resolver de forma independiente al
mismo contacto, la materialización fallida de un `contact_point` detiene el
flujo, sólo `source = hotmart` es autoritativo y tanto el vínculo como las
columnas canónicas del caso quedan protegidos contra updates posteriores.

Una segunda revisión independiente encontró que `checkout_phone` todavía no
participaba de la admisión/tupla SQL y que el vínculo podía borrarse por DML
directo. Se agregaron probes ejecutables para ambos casos, protección contra
`DELETE` y protección del evento fuente frente a mutación o borrado. También se
alineó el contrato con los códigos HTTP efectivos de autenticación,
clasificación y frescura.

La tercera revisión independiente encontró dos inconsistencias más: el binding
extraía dígitos de un `phone` inválido en lugar de usar el mismo fallback a
`checkout_phone`, y un `UPDATE` de columnas no canónicas como `created_at` aún
era posible. El fixture ejecutable ahora combina `phone = invalid123` con un
`checkout_phone` válido y llega a planificación; además se rechaza cualquier
`UPDATE` o `DELETE` del vínculo `cart_abandonment`.

## HTTP real local

Se levantó Uvicorn en `127.0.0.1:18081` con un `SupabaseClient` respaldado por un transporte local controlado que emuló únicamente la RPC de admisión. Se enviaron requests por socket con `curl`; no se usaron secretos reales ni se contactó infraestructura remota.

```text
GET /health
{"status":"ok"} HTTP:200

POST /webhooks/hotmart
{"status":"received","event_id":"local-http-cart-001"} HTTP:202

POST /webhooks/hotmart (buyer con sólo checkout_phone)
{"status":"received","event_id":"local-http-cart-checkout-phone-002"} HTTP:202
```

La primera prueba con timestamp fuera de la ventana produjo correctamente `401 stale_webhook`; se corrigió el fixture con la hora UTC corriente y la repetición fue aceptada. Después de la segunda revisión se repitió la prueba con sólo `checkout_phone`; también fue aceptada. Los procesos Uvicorn se detuvieron al finalizar.

## Límites honestos

- Esta evidencia no prueba que la migración esté aplicada en Supabase remoto.
- Esta evidencia no prueba que EasyPanel esté ejecutando este commit.
- Esta evidencia no prueba un webhook emitido por Hotmart real ni la creación remota de caso/plan.
- No se hizo commit, push ni deploy.
- El E2E remoto de aceptación del workstream B sigue pendiente de la integración y despliegue autorizados.
