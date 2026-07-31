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

El modo sombra permanece sin envíos y desactivado por defecto
(`HERMES_SHADOW_ENABLED=false`). Sobre ese resultado existe un modo de respuesta
independiente, también desactivado por defecto
(`CHATWOOT_AUTOMATED_REPLIES_ENABLED=false`). Al habilitarlo, el bridge vuelve a
consultar etiquetas e historial inmediatamente antes de crear el mensaje como
el AgentBot configurado. No envía si la automatización está pausada, si la
conversación avanzó o si intervino una persona. Cada entrega incluye un marcador
estable en `content_attributes`; los reintentos y solicitudes concurrentes
consultan ese marcador para no crear una segunda respuesta.

El procesamiento es síncrono: el bridge responde después de persistir un
resultado terminal y, cuando corresponde, después de que Chatwoot acepta o
bloquea la respuesta. Si encuentra una captura previa sin resultado, reintenta
con la misma clave idempotente.

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
# Mantener CHATWOOT_AUTOMATED_REPLIES_ENABLED=false hasta completar una
# verificación controlada con el JID autorizado.
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
