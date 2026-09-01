# Contrato de runtime por aliada comercial v1

- Estado: implementado localmente para configuración y readiness; admisión ATT1 bloqueada
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

El parser puede construir un evento canónico con `tenant_ref` y `funnel_ref` del manifiesto. La RPC durable vigente todavía es específica de Johanna; por eso `LEAD_PRECHECKOUT_ENABLED=true` con un manifiesto no legado impide iniciar el runtime.

### Hotmart pago fallido

Producto y oferta deben coincidir con `hotmart_product_id` y `offer_code`. Una configuración ATT1 rechaza eventos Johanna y viceversa durante parsing. La RPC durable vigente sigue siendo exclusiva de Johanna; `JOHANNA_PAYMENT_FAILURE_HOTMART_ENABLED=true` con un manifiesto no legado impide iniciar.

### Chatwoot inbound

Account, inbox, scope key y scope version del manifiesto se validan localmente. Como la cadena heredada de admisión, agente, stops y respuestas aún no está completamente parametrizada, `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, Cut B o respuestas automatizadas no pueden habilitarse con un manifiesto no legado.

## Compatibilidad y prohibiciones

- Sin manifiesto sólo se admite compatibilidad legada con Chatwoot account `1` e inbox `9` o no configurado.
- Cualquier otro account/inbox sin manifiesto impide iniciar.
- La procedencia del manifiesto se conserva por separado de sus valores. Un archivo suministrado nunca entra en compatibilidad legada, aunque copie exactamente todos los valores Johanna.
- El estado `approved` no equivale a `active`.
- Binding activo no equivale a autorización de contacto, activación comercial, deploy o envío real.
- Para cualquier manifiesto suministrado, startup exige que cada flag booleano sea exactamente `False` y rechaza cualquier `HOTMART_HOTTOK` configurado. Esto mantiene fuera de alcance todos los receptores, admisiones, workers, agentes, controles y efectos heredados hasta parametrizarlos y verificarlos específicamente.
- Las rutas outbound heredadas siguen fuera de este contrato.
