# AI Appointment Setter

Puente conversacional entre Chatwoot, Hermes Agent y Evolution API para agentes comerciales por WhatsApp.

## Primer hito

Recibir webhooks `message_created` de Chatwoot, verificar su firma, aceptar exclusivamente mensajes entrantes del JID de prueba autorizado y capturar el payload para diseñar la integración real con Hermes.

## Stack conocido

- Chatwoot Community Edition 4.13.0
- Evolution API 2.3.7
- Hermes Agent (profiles especializados)
- Supabase Cloud (integración posterior)

## Estado

El receptor seguro, la captura privada y la pausa por intervención humana están
implementados. El bridge dispone además de un modo sombra opcional que:

1. recupera hasta 20 mensajes canónicos desde Chatwoot y los trunca en el ID
   exacto del webhook, sin incluir mensajes posteriores;
2. conserva sólo mensajes públicos del prospecto y del AgentBot propio;
3. invoca por HTTP al profile Hermes `agente-comercial`;
4. valida estrictamente la propuesta JSON;
5. guarda el resultado privado en `SHADOW_DIR`.

El modo sombra no contiene ninguna operación de envío y permanece desactivado
por defecto (`HERMES_SHADOW_ENABLED=false`). El procesamiento es síncrono: el
bridge responde después de persistir un resultado terminal. Si encuentra una
captura previa sin resultado, reintenta con la misma clave idempotente.

`HERMES_API_BASE_URL` debe usar HTTPS. Se permite HTTP únicamente para loopback
o para el DNS interno exacto `hermes`, por ejemplo `http://hermes:8643/v1`.
Si Chatwoot no devuelve el mensaje originador dentro del historial acotado, el
bridge registra un fallo sombra y no invoca Hermes con contexto ambiguo.

## Desarrollo

```bash
uv sync
uv run pytest
```

## Ejecutar localmente

```bash
cp .env.example .env
# Completar las variables requeridas de Chatwoot.
# Mantener HERMES_SHADOW_ENABLED=false hasta disponer de un API Server
# autenticado y alcanzable para el profile agente-comercial.
set -a && . ./.env && set +a
uv run uvicorn bridge.app:build_app --factory --host 0.0.0.0 --port 8000
```

Verificación:

```bash
curl http://localhost:8000/health
```

## Docker Compose

```bash
docker compose up --build
```

La guía de conexión se encuentra en `docs/chatwoot-webhook.md`.
