# Evidencia local de destinatarios dinámicos Johanna

- **Fecha:** 2026-08-26
- **Estado:** implementación local; pendiente de publicación, migración y despliegue
- **Branch:** `feat/johanna-dynamic-recipients`
- **No incluyó:** llamadas productivas, webhooks reales, mensajes, cambios de EasyPanel ni mutaciones en Supabase Cloud

## Cambio ejercitado

- `ALLOWED_WHATSAPP_JID` es opcional para el runtime productivo dedicado.
- `.env.example` lo declara vacío y Compose usa `${ALLOWED_WHATSAPP_JID:-}`;
- inbound scoped usa el `expected_jid` de cada conversación autorizada `1/9`;
- carrito automático llama `begin_johanna_abandonment_hotmart_auto_v2` sin enviar un teléfono;
- Supabase deriva `normalized_phone` desde la intención durable;
- el bridge crea un sender efímero fenced exactamente al teléfono autorizado;
- pago fallido conserva el mismo patrón dinámico;
- endpoints manuales/test y motores legacy continúan fail-closed con JID fijo.

## Evidencia TDD focal

Los trazadores RED fallaron por cada fence anterior y quedaron GREEN después del cambio:

```text
uv run pytest -q \
  tests/test_hotmart_webhook.py::test_hotmart_auto_uses_durable_recipient_without_fixed_allowed_jid \
  tests/test_hotmart_webhook.py::test_hotmart_auto_factory_fences_sender_to_durable_recipient \
  tests/test_deployment_config.py::test_deployment_does_not_require_fixed_allowed_jid_for_dynamic_routes \
  tests/test_dynamic_recipient_chatwoot.py \
  tests/test_johanna_dynamic_recipients_migration.py

resultado: 6 passed
```

## Evidencia SQL física

El validador aplicó baseline y todas las migraciones en PGlite. Creó dos intenciones, submissions y eventos Hotmart con teléfonos distintos; correlacionó cada evento y ejecutó el RPC V2 para ambos.

```text
JOHANNA_DYNAMIC_RECIPIENTS_SQL_OK
```

Se verificó que existieran exactamente dos commands, cada uno con el teléfono de su propia intención. El caller no aportó ningún destinatario al RPC V2.

El paquete SQL canónico completo terminó en PASS:

```text
npm test
JOHANNA_DYNAMIC_RECIPIENTS_SQL_OK
acl_hardening=OK positive_control_leaks=6 public_functions=113 service_entrypoints=46
```

El inventario ACL confirmó:

- cero `EXECUTE` para `anon`/`authenticated`;
- cero triggers ejecutables por `service_role`;
- cero diferencias contra la allowlist;
- el nuevo RPC V2 disponible únicamente para `service_role`.

## Gates completos y HTTP local

```text
uv run pytest -q
resultado: PASS

uv build
resultado: PASS

git diff --check
resultado: PASS
```

Se arrancó el entrypoint real sin `ALLOWED_WHATSAPP_JID`, con todos los efectos
default-off y URLs ficticias. La observación por TCP local fue:

```text
/health 200 {"status":"ok"}
/ready 200 {"status":"ready","pilot_boundary":"disabled","automation_state":"default_off","reason_code":"pilot_boundary_disabled"}
```

El proceso Uvicorn completó startup y shutdown. No se llamó a Chatwoot, Hotmart,
Supabase Cloud ni Hermes.

## Límites

- No demuestra migración aplicada en Supabase Cloud.
- No demuestra imagen publicada o desplegada.
- No demuestra tráfico real de dos leads ni aceptación física de mensajes.
- No autoriza ejecutar nuevamente el one-shot histórico ni reutilizar contactos de evidencia previa.
