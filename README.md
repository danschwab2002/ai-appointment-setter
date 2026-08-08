# AI Appointment Setter

Bridge conversacional y motor durable de próxima acción para recuperación de carritos Hotmart por WhatsApp.

## Alcance actual

El sistema conecta actualmente:

```text
Hotmart -> bridge/FastAPI -> Supabase -> Hermes Agent -> Chatwoot -> Evolution API -> WhatsApp
WhatsApp -> Evolution API -> Chatwoot -> bridge/FastAPI -> Hermes Agent -> Chatwoot -> WhatsApp
```

El transporte productivo actual es Evolution API detrás de Chatwoot. La frontera de mensajería está abstraída para una migración futura a WhatsApp Business Platform (WABA), incluidos templates aprobados cuando el canal los requiera.

## Estado implementado

### Recuperación de carrito

El endpoint `POST /webhooks/hotmart`:

1. autentica `X-HOTMART-HOTTOK` y aplica anti-replay;
2. acepta eventos Hotmart v2.0.0 `PURCHASE_OUT_OF_SHOPPING_CART`;
3. normaliza identidad y persiste el evento de forma idempotente en Supabase;
4. planifica una próxima acción bajo una política publicada e inmutable;
5. reevalúa autorización, identidad, compra, opt-out, intervención humana, vigencia y límites antes de cada efecto;
6. permite que Hermes redacte únicamente dentro de la decisión autorizada por el bridge;
7. envía el primer contacto o seguimiento por Chatwoot usando el único JID allowlisted durante las pruebas.

El flujo completo Hotmart → primer WhatsApp → respuesta atendida por el mismo agente comercial fue validado E2E. La evidencia sanitizada está en [`docs/operations/2026-08-02-hotmart-recovery-e2e.md`](docs/operations/2026-08-02-hotmart-recovery-e2e.md).

### Conversaciones entrantes

El endpoint `POST /webhooks/chatwoot`:

- verifica autenticación, anti-replay y el JID autorizado;
- persiste la admisión antes de devolver HTTP 202;
- agrupa mensajes públicos entrantes después de 30 segundos de silencio por conversación;
- consulta y valida el historial canónico de Chatwoot;
- invoca al profile Hermes `agente-comercial`;
- vuelve a verificar pausas, intervención humana, avance de conversación e idempotencia antes de responder.

El batching inbound fue validado E2E en producción. Ver [`docs/operations/2026-08-07-chatwoot-inbound-batching-e2e.md`](docs/operations/2026-08-07-chatwoot-inbound-batching-e2e.md).

### División de respuestas salientes

La división opcional de una respuesta lógica en 1–4 burbujas está implementada y validada localmente. Conserva el texto original, persiste un manifiesto durable y reautoriza cada parte antes del envío.

Continúa apagada por defecto con `CHATWOOT_REPLY_SPLITTER_ENABLED=false` y todavía no tiene evidencia de despliegue ni E2E real por WhatsApp. Ver [`docs/design/outbound-reply-splitting-mvp.md`](docs/design/outbound-reply-splitting-mvp.md).

## Fronteras de responsabilidad

- **Hotmart:** fuente del evento de abandono de carrito aceptado por el receptor actual.
- **Supabase/Postgres:** fuente canónica de casos, políticas, secuencias, próximas acciones, leases y auditoría.
- **Chatwoot:** fuente canónica de conversaciones, mensajes, actores e intervención humana.
- **Bridge:** autenticación, persistencia, autorización, idempotencia, planificación y ejecución de efectos.
- **Hermes:** estrategia y redacción dentro de opciones ya permitidas; no autoriza envíos ni escribe en Supabase.

Las decisiones principales están documentadas en [`docs/architecture.md`](docs/architecture.md) y [`docs/decisions/`](docs/decisions/).

## Stack

- Python 3.12+
- FastAPI y HTTPX
- Supabase Cloud / Postgres / PostgREST
- Chatwoot Community Edition
- Hermes Agent con profiles especializados
- Evolution API como transporte actual
- `uv` para dependencias, ejecución y pruebas

## Desarrollo local

```bash
uv sync
uv run pytest
```

La configuración de ejemplo está en [`.env.example`](.env.example). Los secretos deben existir únicamente en `.env` o en el gestor de secretos del despliegue y nunca deben agregarse a Git.

Para iniciar el bridge:

```bash
cp .env.example .env
# Completar solamente con credenciales y valores del entorno controlado.
set -a && . ./.env && set +a
uv run uvicorn bridge.app:build_app --factory --host 0.0.0.0 --port 8000
```

Verificación básica:

```bash
curl http://localhost:8000/health
```

## Activación segura

Los efectos externos permanecen separados mediante feature flags. En un entorno nuevo se deben mantener apagados hasta verificar migraciones, credenciales, el JID allowlisted y las dependencias reales:

```text
HERMES_SHADOW_ENABLED=false
CHATWOOT_AUTOMATED_REPLIES_ENABLED=false
CHATWOOT_REPLY_SPLITTER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

No debe habilitarse mensajería saliente sólo porque el servicio responda `/health`. La guía de conexión de Chatwoot está en [`docs/chatwoot-webhook.md`](docs/chatwoot-webhook.md), y la evidencia operativa vigente vive en [`docs/operations/`](docs/operations/).
